"""Independent C0 acceptance vectors. Synthetic evidence grants no permission."""
import builtins
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from src.execution.contracts import (
    ValidationError, canonical_sha256, decode_record, validate_record,
)

_PATH = Path(__file__).parents[1] / 'fixtures/execution/contracts.json'
_ROWS = json.loads(_PATH.read_text(encoding='utf-8'))
_IDS = [r['kind'] + ':' + r['name'] for r in _ROWS]


def _hash(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _sample(kind, name=None):
    return copy.deepcopy(next(r['value'] for r in _ROWS
                             if r['kind'] == kind and (name is None or r['name'] == name)))


def _rehash(v):
    for field in ('receipt_id', 'permit_id', 'evidence_sha256'):
        if field in v:
            v[field] = _hash({k: x for k, x in v.items() if k != field})
            return v
    return v


@pytest.mark.parametrize('row', _ROWS, ids=_IDS)
def test_contract_roundtrip_all_kinds_and_unions(row):
    original = row['value']
    validated = validate_record(row['kind'], original)
    decoded = decode_record(row['kind'], json.dumps(original, ensure_ascii=False).encode())
    assert validated == decoded == original
    assert validated is not original and decoded is not original
    bad = copy.deepcopy(original)
    bad['unsupported'] = 1
    with pytest.raises(ValidationError):
        validate_record(row['kind'], bad)
    # Every typed object in every union fixture rejects an extra and a missing
    # member. JSON-valued receipt differences are intentionally not typed here.
    def paths(value, path=()):
        if type(value) is dict:
            yield path
            for key, item in value.items(): yield from paths(item, path+(key,))
        elif type(value) is list:
            for index, item in enumerate(value): yield from paths(item, path+(index,))
    for path in paths(original):
        for mutation in ('extra','missing'):
            candidate=copy.deepcopy(original); node=candidate
            for part in path: node=node[part]
            if mutation=='extra':node['unsupported_nested']=None
            elif node:node.pop(next(iter(node)))
            else:continue
            with pytest.raises(ValidationError):validate_record(row['kind'],candidate)


_BAD_RAW = [
    b'{"schema_version":1,"schema_version":1}',
    b'{"x":{"same":1,"same":2}}', b'{"x":1.0}', b'{"x":1e0}',
    b'{"x":NaN}', b'{"x":Infinity}', b'\xef\xbb\xbf{}', b'\xff',
    b'[]', b'{} trailing', b'{"x":9223372036854775808}',
    b'{"x":-1}', b'{"x":"\\ud800"}', b' ' * (1048576 + 1),
]
_BAD_FIELDS = [
    ('schema_version', True), ('schema_version', 1.0), ('schema_version', 2),
    ('limit_price', '1.0'), ('limit_price', '01'), ('limit_price', '+1'),
    ('limit_price', '1e0'), ('limit_price', '.1'), ('limit_price', '1.'),
    ('limit_price', '-0'), ('limit_price', 'NaN'), ('limit_price', '١'),
    ('limit_price', '0.0000000000000000001'), ('limit_price', '1' * 39),
    ('limit_price', 0.3), ('limit_price', '0'), ('shares', '-1'),
    ('created_at', '2026-10-30T00:00:00'),
    ('created_at', '2026-02-30T00:00:00Z'),
    ('created_at', '2026-10-30T00:00:00.1234567Z'),
    ('created_at', '2026-10-30T00:00:00+00:00'),
    ('side', 'SELL'), ('chain_id', True), ('chain_id', -1),
    ('chain_id', 2**63), ('command_id', '__user'),
    ('proposal_id', ''), ('proposal_id', 'x' * 4097),
    ('money_quantum', '0.02'), ('money_quantum', '0'),
    ('proposal_id', 'UNKNOWN'),
]


@pytest.mark.parametrize('case', list(range(len(_BAD_RAW) + len(_BAD_FIELDS) + 10)))
def test_contract_rejects_noncanonical_numeric_and_duplicate_json(case):
    if case < len(_BAD_RAW):
        with pytest.raises(ValidationError):
            decode_record('proposal', _BAD_RAW[case])
        return
    case -= len(_BAD_RAW)
    if case < len(_BAD_FIELDS):
        value = _sample('proposal', 'submit')
        key, value[key] = _BAD_FIELDS[case]
        with pytest.raises(ValidationError):
            validate_record('proposal', value)
        return
    case -= len(_BAD_FIELDS)
    if case == 0:
        value = _sample('account_snapshot'); value['source']['extra'] = 'bad'
        with pytest.raises(ValidationError): validate_record('account_snapshot', value)
    elif case == 1:
        with pytest.raises(ValidationError): canonical_sha256({'x': float('nan')})
    elif case == 2:
        with pytest.raises(ValidationError): canonical_sha256({'x': [0] * 10001})
    elif case == 3:
        value = {}; cursor = value
        for _ in range(32): cursor['x'] = {}; cursor = cursor['x']
        with pytest.raises(ValidationError): canonical_sha256(value)
    elif case == 4:
        value = {}; cursor = value
        for _ in range(31): cursor['x'] = {}; cursor = cursor['x']
        assert canonical_sha256(value) == _hash(value)
    elif case == 5:
        assert canonical_sha256({'x': ['a'] * 10000}) == _hash({'x': ['a'] * 10000})
    elif case == 6:
        value = _sample('proposal', 'submit'); value.update(limit_price='1' * 38, chain_id=2**63-1, created_at='2026-10-30T00:00:00.123456Z', proposal_id='x'*4096)
        assert validate_record('proposal', value) == value
    elif case == 7:
        with pytest.raises(ValidationError): validate_record('unknown', {})
    elif case == 8:
        value=_sample('book_snapshot');value['minimum_unit']='UNKNOWN'
        with pytest.raises(ValidationError):validate_record('book_snapshot',value)
    else:
        value=_sample('book_snapshot');value.update(complete=False,minimum_unit='UNKNOWN',tick_size='UNKNOWN')
        assert validate_record('book_snapshot',value)==value


@pytest.mark.parametrize('target', ['command-price', 'command-account', 'permit-binding', 'permit-context', 'receipt', 'settlement', 'event-payload', 'baseline', 'unicode', 'empty-json'])
def test_hash_preimages_are_non_circular_and_bound(target):
    if target.startswith('command'):
        v = _sample('command'); keys = 'account_ref venue condition_id token_id side order_type limit_price shares fee_bound max_all_in_cost collateral_asset chain_id unit_scale price_unit shares_unit money_unit money_quantum expires_at protocol_evidence_sha'.split()
        assert v['canonical_payload_sha256'] == _hash({k:v[k] for k in keys})
        v['limit_price' if target.endswith('price') else 'account_ref'] = '0.4' if target.endswith('price') else 'another-account'
        with pytest.raises(ValidationError): validate_record('command', v)
        v['canonical_payload_sha256'] = _hash({k:v[k] for k in keys})
        assert validate_record('command', v) == v
    elif target.startswith('permit'):
        v = _sample('permit', 'submit'); assert v['permit_id'] == _hash({k:x for k,x in v.items() if k!='permit_id'})
        if target.endswith('binding'): v['reserve_context']['store_binding']['host_ref']='other-host'
        else: v['reserve_context']['source_global_version'] += 1
        with pytest.raises(ValidationError): validate_record('permit', v)
        assert _hash({k:x for k,x in v.items() if k!='permit_id'}) != _sample('permit','submit')['permit_id']
    elif target == 'receipt':
        v = _sample('receipt', 'reconciliation'); assert v['receipt_id'] == _hash({k:x for k,x in v.items() if k!='receipt_id'})
        v['limitations'].append('NEW_LIMITATION')
        with pytest.raises(ValidationError): validate_record('receipt', v)
        _rehash(v); assert validate_record('receipt', validate_record('receipt', v)) == v
    elif target == 'settlement':
        v = _sample('settlement_evidence'); assert v['evidence_sha256'] == _hash({k:x for k,x in v.items() if k!='evidence_sha256'})
        v['owner_action_ref']='different-owner-evidence'
        with pytest.raises(ValidationError): validate_record('settlement_evidence', v)
        _rehash(v); assert validate_record('settlement_evidence', v) == v
    elif target == 'event-payload':
        v = _sample('store_event','STOP_SET'); assert v['payload_sha256'] == _hash(v['payload'])
        v['payload']['reason']='ANOTHER_REASON'
        with pytest.raises(ValidationError): validate_record('store_event', v)
    elif target == 'baseline':
        v=_sample('account_snapshot');v['projection']['baseline_sha256']='UNKNOWN'; baseline=_hash(v)
        v['projection']['baseline_sha256']=baseline
        assert validate_record('account_snapshot',v)==v
        assert _hash(dict(v,projection=dict(v['projection'],baseline_sha256='UNKNOWN')))==baseline
    elif target=='unicode':
        assert canonical_sha256({'x':'é'}) == _hash({'x':'é'})
        assert canonical_sha256({'x':'é'}) != canonical_sha256({'x':'e\u0301'})
    else:
        assert canonical_sha256({'':'', 'marker':'UNKNOWN'})==_hash({'':'', 'marker':'UNKNOWN'})


@pytest.mark.parametrize('row', _ROWS, ids=_IDS)
def test_contract_inputs_are_not_mutated(row):
    original = copy.deepcopy(row['value']); before=copy.deepcopy(original)
    result = validate_record(row['kind'], original)
    assert original == before
    def walk(a,b):
        if type(a) in (dict,list):
            assert a is not b
            if isinstance(a,dict):
                for k in a: walk(a[k],b[k])
            else:
                for x,y in zip(a,b): walk(x,y)
    walk(original,result)
    invalid=copy.deepcopy(original);invalid['extra']=[];before=copy.deepcopy(invalid)
    with pytest.raises(ValidationError):validate_record(row['kind'],invalid)
    assert invalid == before


@pytest.mark.parametrize('operation', ['import', 'validate', 'decode', 'hash'])
def test_contract_import_has_zero_io(monkeypatch, operation):
    # Harness code loading and fixture reads precede application tripwires.
    source = (Path(__file__).parents[2] / 'src/execution/contracts.py').read_text(encoding='utf-8')
    code = compile(source, '<C0 isolated import>', 'exec')
    v=_sample('permit','submit'); raw=json.dumps(v).encode(); attempts=[]
    import copy as _copy, datetime as _dt, decimal as _decimal, re as _re
    def deny(*a,**kw):
        attempts.append('io'); raise RuntimeError('forbidden application IO')
    with monkeypatch.context() as m:
        for module, names in [(builtins,['open']), (io,['open']), (os,['open','getenv','listdir','stat']), (socket,['gethostname','getaddrinfo','create_connection','socket']), (subprocess,['Popen','run'])]:
            for name in names:m.setattr(module,name,deny)
        m.setattr(os, 'environ', type('BlockedEnv', (), {'__getitem__':deny,'get':deny,'__iter__':deny,'__contains__':deny})())
        namespace={'__name__':'c0_isolated'};exec(code,namespace)
        if operation=='validate': assert namespace['validate_record']('permit',v)==v
        elif operation=='decode':assert namespace['decode_record']('permit',raw)==v
        elif operation=='hash':assert namespace['canonical_sha256'](v)==_hash(v)
    assert attempts == []


@pytest.mark.parametrize('case', ['total','reserved','available','additional','overlap','negative','duplicate-external','unknown-complete','hold-unknown','hold-negative','hold-negative-source','precision38','precision38-wrong','source-pair','coverage','positive'])
def test_account_cash_identity_and_overlap_schema(case):
    v=_sample('account_snapshot'); valid=False
    key={'total':'cash_total','reserved':'cash_reserved','available':'cash_available','additional':'cash_local_additional_reserved'}.get(case)
    if key:v[key]='9'
    elif case=='overlap':v['reserve_groups'][0]['external_overlap_amount']='4'
    elif case=='negative':v['cash_external_available']='-1'
    elif case=='duplicate-external':v['reserve_groups'].append(copy.deepcopy(v['reserve_groups'][0]))
    elif case=='unknown-complete':v['cash_total']='UNKNOWN'
    elif case=='hold-unknown':v.update(status='HOLD',cash_total='UNKNOWN',problems=['CASH_UNKNOWN'],reconcile_verdict='HOLD',stop_new_orders=True);valid=True
    elif case=='hold-negative':v.update(status='HOLD',cash_total='2',cash_external_available='0',cash_available='-1',problems=['CASH_DEFICIT'],reconcile_verdict='HOLD',stop_new_orders=True);valid=True
    elif case=='hold-negative-source':v.update(status='HOLD',cash_total='1',cash_external_available='-1',cash_available='-2',problems=['CASH_DEFICIT'],reconcile_verdict='HOLD',stop_new_orders=True)
    elif case in ('precision38','precision38-wrong'):
        v.update(cash_total='12345678901234567890123456789012345678',cash_external_available='12345678901234567890123456789012345676',cash_available='12345678901234567890123456789012345675');valid=True
        # A default precision28 implementation silently loses these last digits.
        v['reserve_groups'][0]['local_amount']='12345678901234567890123456789012345678'
        v['reserve_groups'][0]['external_overlap_amount']='12345678901234567890123456789012345677'
        if case=='precision38-wrong':v['cash_available']='12345678901234567890123456789012345674';valid=False
    elif case=='source-pair':v['projection']['source_version']=2
    elif case=='coverage':v['coverage']['cash_complete']=False
    else:valid=True
    if valid:assert validate_record('account_snapshot',v)==v
    else:
        with pytest.raises(ValidationError):validate_record('account_snapshot',v)


@pytest.mark.parametrize('case', ['extra','unknown-cost','unknown-fee','unknown-cash','hidden-summary','synthetic-full','missing-settlement','cancel-empty','unknown-settlement','reconciliation-unknown','valid-partial'])
def test_receipt_subtypes_cannot_hide_unknowns(case):
    v=_sample('receipt','execution_closeout'); valid=False
    if case=='extra':v['net_profit']='1'
    elif case in ('unknown-cost','unknown-fee','unknown-cash'):v[{'unknown-cost':'actual_cost','unknown-fee':'actual_fee','unknown-cash':'cash_delta'}[case]]='UNKNOWN'
    elif case=='hidden-summary':v['command_summaries'][0]['actual_fee']='UNKNOWN'
    elif case=='synthetic-full':v.update(stage='S1',coverage='FULL')
    elif case=='missing-settlement':v.update(stage='S1',evidence_class='EXTERNAL',coverage='FULL')
    elif case=='cancel-empty':v.update(cancel_confirmed=True)
    elif case=='unknown-settlement':v=_sample('receipt','settlement_validation');v['verified_net_collateral_credit']='UNKNOWN'
    elif case=='reconciliation-unknown':v=_sample('receipt','reconciliation');v['checked_dimensions']['cash']='UNKNOWN'
    else:v.update(verdict='UNRESOLVED',actual_fee='UNKNOWN',unknowns=['ACTUAL_FEE_UNKNOWN'],problems=['ACTUAL_FEE_UNKNOWN']);valid=True
    _rehash(v)
    if valid:assert validate_record('receipt',v)==v
    else:
        with pytest.raises(ValidationError):validate_record('receipt',v)
