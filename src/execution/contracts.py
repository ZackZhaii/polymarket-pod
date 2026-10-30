"""Pure C0 schemas. Parsed records confer no authority or external truth.

Hashes bind supplied bytes. Store, ownership and evidence verification belongs
to consumers. No function here resolves paths or consults external state.
"""
import copy as _copy
import datetime as _datetime
import decimal as _decimal
import hashlib as _hashlib
import json as _json
import re as _re

Record = dict[str, object]


class ValidationError(ValueError):
    """A record violates its exact schema or same-record relationships."""


_DECIMAL = _re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z')
_TIME = _re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z')
_MODES = ('channel_test', 'strategy_live')
_LIFECYCLE = tuple('RESERVED SUBMITTING ACK_OPEN SUBMIT_UNKNOWN PARTIALLY_FILLED CANCEL_REQUESTED CANCELED_CONFIRMED FILLED_CONFIRMED REJECTED_CONFIRMED RECONCILIATION_HOLD'.split())
_FINALITY = ('FINALIZED', 'PENDING', 'REORGED', 'UNKNOWN')
_UNKNOWN = 'UNKNOWN'


def _fail(path, message):
    raise ValidationError(f'{path}: {message}')


def _u(spec): return ('unknown', spec)
def _n(spec): return ('nullable', spec)
def _l(spec, unique=False): return ('list', spec, unique)
def _e(*values): return ('enum', values)
def _f(names, spec='S'): return dict.fromkeys(names.split(), spec)


_SCOPE = _f('account_ref venue')
_UNITS = {**_f('collateral_asset price_unit shares_unit money_unit', _u('S')),
          **_f('chain_id unit_scale', _u('I')), 'money_quantum': _u('Q')}
_IDENTITY = _f('command_id run_id intent_id decision_id idempotency_key')
_SIZING = {**_f('condition_id token_id'), 'side': _e('BUY'), 'order_type': _e('LIMIT'),
           **_f('limit_price shares', 'P'), **_f('fee_bound max_all_in_cost', 'D')}
_STRATEGY = {**_f('intent_sha256 strategy_sha model_sha', _u('H')), 'data_cutoff': _u('T')}
_CAPS = {**_f('max_order_all_in max_total_spend max_cumulative_loss max_shares max_limit_price max_fee', 'D'), 'max_attempts':'I'}
_VERSION = {'schema_version': ('literal', 1)}
_SCHEMAS = {
    'Asset': {'chain_id':'I', 'asset_id':'S', 'unit_scale':'I'},
    'StoreBinding': {**_f('host_ref canonical_store_path'), 'store_id':'UUID'},
    'RiskBasis': {**_f('spend_committed loss_realized positions_max_loss open_orders_max_loss unknown_submit_max_loss unfinalized_fee_bound', _u('D')), 'disjoint_verified':'B','evidence_ref':_u('S')},
    'Usage': {'approval_receipt_sha':'H','attempts_used':'I', **_f('cumulative_spend cumulative_loss','D'),'risk_basis':'RiskBasis'},
    'Watermark': {**_f('stream_id cursor baseline_approval_ref',_u('S')),**_f('source_sequence block_number',_u('I')),**_f('block_hash included_event_ids_sha256',_u('H')),'finalized_through':_u('T')},
    'AccountSource': {'namespace':'S', **_f('identity_contract_ref account_product_ref asset_contract_ref cash_semantics_ref finality_policy_ref',_u('S')),'identity_contract_sha256':_u('H')},
    'AccountCoverage': {**_f('cash_complete positions_complete open_orders_complete fills_complete funding_complete fees_complete pagination_complete cursors_contiguous','B'),'pages':'I','next_cursor':_n('S'),'missing_ranges':_l('S')},
    'Position': {**_f('condition_id token_id position_evidence_ref'),'outcome_index':'I',**_f('settled_shares pending_in_shares pending_out_shares available_shares reserved_shares','SD')},
    'AccountOrder': {**_f('exchange_order_id condition_id token_id status evidence_ref'),'command_id':_n('S'),'ownership':_e('LOCAL','EXTERNAL','UNKNOWN'),'side':_e('BUY','SELL'),**_f('limit_price cumulative_fill_shares remaining_shares','D'),**_f('fee_bound_remaining external_reserved_amount',_u('D')),'evidence_sha256':'H'},
    'ReserveGroup': {**_f('reservation_id command_id'),'exchange_order_id':_n('S'),'liability_kind':_e('OPEN_ORDER','SUBMIT_UNKNOWN','PENDING_DEBIT','PENDING_FEE'),'asset_key':'Asset','local_amount':'D','external_reservation_ids':_l('S',True),'external_overlap_amount':_u('D'),'mapping_evidence_ref':_u('S'),'source_version':'I','source_sha256':'H'},
    'Projection': {**_f('baseline_sha256 journal_event_ids_sha256','H'),**_f('applied_event_ids duplicate_event_ids pending_event_ids conflict_event_ids',_l('S',True)),'source_version':_u('I'),'source_sha256':_u('H')},
    'KnownOrder': {**_f('command_id exchange_order_id account_ref venue condition_id token_id evidence_ref'),'target_proposal_sha':'H','ownership_verified':'B','evidence_sha256':'H'},
    'CancelScope': {'allowed':'B','command_ids':_l('S',True),'exchange_order_ids':_l('S',True),'expires_at':'T','revoked':'B'},
    'StrategyEvidence': {'verified':'B','promotion_receipt_ref':_u('S'),**_f('promotion_receipt_sha strategy_sha model_sha',_u('H')),'data_cutoff':_u('T'),'net_costs_complete':'B'},
    'SelfTrade': {'verified':'B','conflict':'B','evidence_ref':_u('S')},
    'ObservationSource': {'kind':_e('fixture','venue'),'ref':'S','schema_sha256':'H'},
    'ObservationCoverage': {'scope':_e('single_order','account_all'),'account_scope_verified':'B',**_f('from_cursor to_cursor watermark',_n('S')),'has_more':'B'},
    'OrderRow': {**_f('exchange_order_id account_ref venue asset_id condition_id outcome_id order_type status'),'command_id':_n('S'),'market_protocol':_e('ctf','v2','fixture'),'asset_kind':_e('ctf_token','position_v2','fixture'),'side':_e('BUY','SELL'),**_f('limit_price original_shares cumulative_fill_shares remaining_shares','D'),'created_at':'T','expires_at':_n('T'),'external_event_ids':_l('S',True)},
    'FillRow': {**_f('external_event_id exchange_order_id account_ref venue asset_id condition_id outcome_id evidence_ref'),'market_protocol':_e('ctf','v2','fixture'),'asset_kind':_e('ctf_token','position_v2','fixture'),'side':_e('BUY','SELL'),**_f('shares price cash_amount','D'),**_f('cash_asset cash_unit fee_asset fee_unit transaction_hash',_n('S')),'fee_amount':_n('D'),'fee_status':_e('KNOWN','UNKNOWN'),'source_timestamp':'T',**_f('chain_id block_number',_n('I')),'chain_finality':_e('CONFIRMED','PENDING','FAILED','UNKNOWN'),'evidence_sha256':'H'},
    'OrderRef': {**_f('command_id account_ref venue exchange_order_id'),'canonical_payload_sha256':'H'},
    'TokenDelta': {**_f('condition_id token_id'),**_f('settled_delta pending_in_delta pending_out_delta','SD')},
    'EventFill': {**_f('fill_id condition_id token_id'),'side':_e('BUY','SELL'),**_f('shares price gross_cash_amount cumulative_fill_shares remaining_shares','D'),'fee_event_ids':_l('S',True)},
    'EventFee': {**_f('fee_id linked_event_id'),'amount':_u('D'),'asset':'Asset','included_in_cash_delta':'B','finality':_e(*_FINALITY)},
    'ReservationChange': {'reservation_id':'S','operation':_e('CREATE','CONSUME','RELEASE','RECLASSIFY'),'amount':'D','external_overlap_amount':'D','source_version':'I','source_sha256':'H'},
    'SettlementFee': {'fee_id':'S','amount':_u('D'),'asset':'Asset','included_in_balance_delta':'B','evidence_ref':'S'},
    'SourceRef': {**_f('source ref'),'sha256':'H','observed_at':'T','complete':'B'},
    'EvidenceRef': {'ref':'S','sha256':'H'},
    'Difference': {**_f('dimension key evidence_ref'), 'local_value':'JSON','external_value':'JSON'},
    'FeeTotal': {'asset':'Asset','amount':_u('D')},
    'CommandSummary': {**_f('command_id run_id approval_receipt_ref'),'mode':_e(*_MODES),'intent_sha256':_u('H'),**_f('proposal_sha256 policy_sha256 approval_sha256','H'),'final_state':_e(*_LIFECYCLE),**_f('internal_event_ids external_event_ids',_l('S',True)),**_f('cancel_requested cancel_confirmed','B'),**_f('actual_cost actual_fee',_u('D'))},
    'CommandAccounting': {'asset':'Asset',**_f('actual_cost actual_fee',_u('D')),'cash_delta':_u('SD'),**_f('cost_complete fees_complete cash_complete','B'),**_f('fill_ids fee_event_ids account_event_ids',_l('S',True)),'unknowns':_l('R',True)},
    'Reservation': {**_f('reservation_id command_id account_ref'),'asset_key':'Asset','exchange_order_id':_n('S'),**_f('remaining_cost fee_bound_remaining','D'),'reserve_groups':_l('ReserveGroup')},
    'EventRef': {'event_id':'S','version':'I','event_hash':'H'},
}
_SCHEMAS['proposal:submit'] = {**_VERSION, 'proposal_id':'S',**_IDENTITY,'action':_e('submit'),'mode':_e(*_MODES),**_SCOPE,**_UNITS,**_SIZING,**_f('created_at expires_at','T'),**_f('account_snapshot_sha book_snapshot_sha policy_sha','H'),**_STRATEGY}
_SCHEMAS['proposal:request_cancel'] = {**_VERSION,'proposal_id':'S',**_IDENTITY,'action':_e('request_cancel'),'mode':_e(*_MODES),**_SCOPE,**_f('created_at expires_at','T'),'policy_sha':'H',**_f('target_command_id target_exchange_order_id'),'target_proposal_sha':'H'}
_SCHEMAS['command'] = {**_VERSION,**_IDENTITY,'mode':_e(*_MODES),**_SCOPE,**_UNITS,**_SIZING,**_f('created_at expires_at','T'),**_STRATEGY,'permit_id':'H',**_f('canonical_payload_sha256 protocol_evidence_sha','H'),'asset_key':'Asset'}
_SCHEMAS['policy'] = {**_VERSION,'policy_id':'S','profile':_e(*_MODES),**_f('new_orders_enabled cancel_enabled strategy_gate_required','B'),**_f('allowed_actions account_refs venues condition_ids token_ids sides',_l('S',True)),**_UNITS,**_CAPS,**_f('account_max_age_seconds book_max_age_seconds approval_max_age_seconds max_clock_skew_seconds','I'),**_f('protocol_evidence_ref boundary_approval_ref',_u('S')),**_f('protocol_evidence_sha boundary_approval_sha',_u('H'))}
_SCHEMAS['approval'] = {**_VERSION,**_f('approval_receipt_ref owner_ref'),'approval_receipt_sha':'H',**_f('owner_approval_verified eligibility_verified custody_verified revoked','B'),'mode':_e(*_MODES),**_SCOPE,**_f('condition_ids token_ids sides allowed_actions',_l('S',True)),**_f('proposal_sha policy_sha protocol_evidence_sha boundary_approval_sha','H'),**_f('issued_at expires_at','T'),**_CAPS,'store_binding':'StoreBinding','cancel_scope':'CancelScope','strategy_evidence':'StrategyEvidence'}
_SCHEMAS['ReserveContext'] = {'proposal':'proposal','account_snapshot':'account_snapshot','book_snapshot':'book_snapshot','approval':'approval','policy':'policy','source_global_version':'I','source_state_sha256':'H','store_binding':'StoreBinding'}
_PERMIT_COMMON = {**_VERSION,**_f('permit_id proposal_sha policy_sha approval_receipt_sha','H'),**_f('command_id approval_receipt_ref'),'mode':_e(*_MODES),**_SCOPE,'allowed_actions':_l('S',True),**_f('issued_at expiry','T'),'gate_result':_e('ALLOW'),'reasons':_l('S')}
_SCHEMAS['permit:submit'] = {**_PERMIT_COMMON,'action':_e('submit'),**_UNITS,**_SIZING,**_f('account_snapshot_sha book_snapshot_sha canonical_payload_sha256','H'),**_f('max_notional max_cumulative_loss','D'),'max_attempts':('literal',1),'expected_state_version':'I','expected_state_sha256':'H','reserve_context':'ReserveContext'}
_SCHEMAS['permit:request_cancel'] = {**_PERMIT_COMMON,'action':_e('request_cancel'),**_f('target_command_id target_exchange_order_id'),'target_proposal_sha':'H'}
_SCHEMAS['account_snapshot'] = {**_VERSION,'snapshot_id':'S','mode':_e(*_MODES),'evidence_class':_e('SYNTHETIC','EXTERNAL'),**_SCOPE,**_UNITS,**_f('asof observed_at valid_until','T'),'status':_e('COMPLETE','HOLD'),'clock_verified':'B','clock_skew_seconds':_u('D'),'source':'AccountSource','source_evidence_ref':'S','source_evidence_sha256':'H','coverage':'AccountCoverage','watermark':'Watermark',**_f('cash_total cash_external_available cash_external_reserved cash_local_additional_reserved cash_reserved',_u('D')),'cash_available':_u('SD'),'positions':_l('Position'),'open_orders':_l('AccountOrder'),'reserve_groups':_l('ReserveGroup'),'funding_events':_l('S',True),'projection':'Projection','problems':_l('R',True),'reconcile_verdict':_e('MATCH','HOLD','UNKNOWN'),'state_version':_u('I'),'state_sha256':_u('H'),'stop_new_orders':'B','unresolved_commands':_l('S',True),'risk_basis':'RiskBasis','approval_usage':_l('Usage'),'known_order_refs':_l('KnownOrder')}
_SCHEMAS['book_snapshot'] = {**_VERSION,**_f('snapshot_id venue condition_id token_id'),**_UNITS,**_f('asof observed_at','T'),**_f('source evidence_ref'),'evidence_sha256':'H',**_f('complete clock_verified','B'),'clock_skew_seconds':_u('D'),**_f('tick_size share_step min_order_size fee_bound',_u('D')),**_f('fee_schedule_sha protocol_evidence_sha',_u('H')),'self_trade_check':'SelfTrade','minimum_unit':_e('money','shares','UNKNOWN')}
_SCHEMAS['observation'] = {**_VERSION,'observation_id':'S','command_id':_n('S'),**_SCOPE,'operation':_e('submit','cancel','order','open_orders','fills'),'source':'ObservationSource','source_timestamp':_n('T'),'observed_at':'T','evidence_ref':'S','evidence_sha256':'H','status':_e('ACCEPTED','REJECTED_RESPONSE','UNKNOWN','CANCEL_REQUESTED','CANCEL_UNKNOWN','ACK_OPEN','PARTIALLY_FILLED','FILLED_CONFIRMED','CANCELED_CONFIRMED','REJECTED_CONFIRMED','ORDER_NOT_FOUND','OK','PARTIAL'),'complete':'B','coverage':'ObservationCoverage','problems':_l('R',True),'payload':'ObservationPayload'}
_SCHEMAS['account_event'] = {**_VERSION,**_f('event_id account_ref venue source_namespace external_event_id identity_contract_ref'),'event_type':_e('FILL','FEE','ORDER_UPDATE','RESERVATION_UPDATE','DEPOSIT','WITHDRAWAL','TRANSFER','REDEMPTION','REVERSAL'),**_f('source_sequence block_number',_n('I')),**_f('source_timestamp observed_at','T'),**_f('command_id exchange_order_id transaction_hash block_hash reverses_event_id',_n('S')),'finality':_e(*_FINALITY),'asset':'Asset','cash_delta':_u('SD'),'token_deltas':_l('TokenDelta'),'fill':_n('EventFill'),'fee':_n('EventFee'),'reservation_change':_n('ReservationChange'),'evidence_ref':'S','evidence_sha256':'H'}
_SCHEMAS['settlement_evidence'] = {**_VERSION,**_f('evidence_id account_ref venue condition_id token_id'),'mode':_e(*_MODES),'asset':'Asset',**_f('shares_redeemed payout_per_share gross_collateral_credit net_collateral_credit token_balance_before token_balance_after collateral_before collateral_after',_u('D')),'fees':_l('SettlementFee'),'external_event_ids':_l('S',True),'transaction_hash':'S',**_f('chain_id block_number',_u('I')),'block_hash':_u('H'),'finality':_e(*_FINALITY),'finality_policy_ref':'S','receipt_status':_e('RESOLVED_ONLY','REDEEMABLE','PENDING','FINALIZED','REORGED','UNKNOWN'),'action_origin':_e('HUMAN_EXTERNAL'),'owner_action_ref':_u('S'),**_f('account_before_sha256 account_after_sha256 evidence_sha256','H'),'source_refs':_l('SourceRef')}
_RECEIPT_HEADER = {**_VERSION,'receipt_type':'S','receipt_id':'H','generated_at':'T','mode':_e(*_MODES),'account_ref':'S','verdict':'S',**_f('unknowns limitations',_l('S',True)),'evidence_refs':_l('EvidenceRef'),'authorization_effect':_e('NONE')}
_SCHEMAS['receipt:reconciliation'] = {**_RECEIPT_HEADER,**_f('local_snapshot_sha256 external_snapshot_sha256','H'),'source_state_version':_u('I'),'source_state_sha256':_u('H'),'common_watermark':'Watermark','checked_dimensions':_f('cash positions open_orders fills funding fees reserves finality',_e('MATCH','MISMATCH','UNKNOWN')),'differences':_l('Difference'),'residual_orders':_l('AccountOrder'),'residual_positions':_l('Position'),'residual_reservations':_l('ReserveGroup'),'problems':_l('R',True),'stop_new_orders':'B','exchange_action':_e('none')}
_SCHEMAS['receipt:settlement_validation'] = {**_RECEIPT_HEADER,'evidence_id':'S',**_f('expected_sha256 evidence_sha256','H'),'settlement_stage':_e('RESOLVED_ONLY','REDEEMABLE','PENDING','CASH_FINAL','REORGED','UNKNOWN'),'cash_credit_verified':'B','verified_net_collateral_credit':_u('D'),'fee_totals_by_asset':_l('FeeTotal'),'external_event_ids':_l('S',True),'finality':_e(*_FINALITY),'problems':_l('R',True),'exchange_action':_e('none')}
_SCHEMAS['receipt:execution_closeout'] = {**_RECEIPT_HEADER,'venue':'S','stage':_e('S0','S1'),'evidence_class':_e('SYNTHETIC','EXTERNAL'),'coverage':_e('PARTIAL','FULL'),**_f('tested_capabilities unverified_capabilities',_l('S',True)),'source_state_version':'I',**_f('source_state_sha256 reconciliation_receipt_sha256','H'),'command_summaries':_l('CommandSummary'),'final_state':'S',**_f('internal_event_ids external_event_ids',_l('S',True)),**_f('new_orders_stopped cancel_requested cancel_confirmed cash_reconciled','B'),**_f('actual_cost actual_fee',_u('D')),'cash_delta':_u('SD'),'asset':'Asset','residual_orders':_l('AccountOrder'),'residual_positions':_l('Position'),'residual_reservations':_l('ReserveGroup'),'reconcile_verdict':_e('MATCH','HOLD'),'control_plane_last_observed_at':_u('T'),'problems':_l('R',True)}
_SCHEMAS['store_event'] = {**_VERSION,**_f('event_id event_type command_id'),'recorded_at':'T','payload':'StorePayload','payload_sha256':'H','source_ref':'S'}
_SCHEMAS['CommandState'] = {'record':'command','permit':'permit','state':_e(*_LIFECYCLE),'last_order_state':_n('S'),'attempt_count':'I',**_f('cumulative_fill_shares remaining_shares','D'),'event_ids':_l('S',True),'accounting_summary':'CommandAccounting'}
_SCHEMAS['store_snapshot'] = {**_VERSION,'store_id':'UUID','store_binding':'StoreBinding','version':'I','state_sha256':'H','new_orders_stopped':'B','stop_reasons':_l('R',True),'commands':_l('CommandState'),'reservations':_l('Reservation'),'account':_u('account_snapshot'),'approval_usage':_l('Usage'),'event_refs':_l('EventRef')}
_SCHEMAS['controller_result'] = {**_VERSION,'command_id':_n('S'),'status':_e(*_LIFECYCLE,'DENIED','OBSERVED','RECOVERED','HOLD'),'store_version':'I','store_state_sha256':'H','reasons':_l('R',True)}
_KINDS = frozenset(('proposal','command','permit','observation','account_snapshot','approval','policy','book_snapshot','account_event','settlement_evidence','receipt','store_event','store_snapshot','controller_result'))
_PAYLOAD_KEYS = tuple('account_ref venue condition_id token_id side order_type limit_price shares fee_bound max_all_in_cost collateral_asset chain_id unit_scale price_unit shares_unit money_unit money_quantum expires_at protocol_evidence_sha'.split())
_OBS_PAYLOADS = {
    'submit': ({'order':_n('OrderRow'),'venue_status':_n('S'),'rejection_code':_n('S')}, ('ACCEPTED','REJECTED_RESPONSE','UNKNOWN')),
    'cancel': ({'exchange_order_id':'S','accepted':_n('B'),'reason_code':_n('S')},('CANCEL_REQUESTED','CANCEL_UNKNOWN')),
    'order': ({'order':_n('OrderRow')},('ACK_OPEN','PARTIALLY_FILLED','FILLED_CONFIRMED','CANCELED_CONFIRMED','REJECTED_CONFIRMED','ORDER_NOT_FOUND','UNKNOWN')),
    'open_orders': ({'orders':_l('OrderRow')},('OK','PARTIAL','UNKNOWN')),
    'fills': ({'fills':_l('FillRow')},('OK','PARTIAL','UNKNOWN')),
}
_STORE_PAYLOADS = {
    'STOP_SET': {'reason':'R'},
    'ACCOUNT_BASELINED': {'account_snapshot':'account_snapshot','approval':'approval'},
    'ACCOUNT_RECONCILED': {'account_snapshot':'account_snapshot','reconciliation':'receipt','account_events':_l('account_event')},
    'APPROVAL_REVOKED': {'approval_receipt_sha':'H','scope':_e('all','submit','cancel'),'reason':'R'},
    'SUBMISSION_STARTED': {'attempt_no':('literal',1),'command_sha256':'H','permit_sha256':'H'},
    'SUBMISSION_UNKNOWN': {'reason':'R','evidence_ref':'S'},
    'ORDER_OBSERVED': {'observation':'observation','account_events':_l('account_event')},
    'CANCEL_OBSERVED': {'observation':'observation','account_events':_l('account_event')},
    'CANCEL_REQUESTED': {'order_ref':'OrderRef','permit':'permit'},
    'UNSENT_REJECTED': {'reason':'R','attempt_no':('literal',0)},
    'EVIDENCE_CONFLICT': {'reason':'R','evidence_hashes':_l('H',True)},
    'RESERVE_ACCEPTED': {'command':'command','permit':'permit','reservation':'Reservation'},
}


def _primitive(value, path='$', depth=0, active=None):
    """Bound JSON recursively, including cycles, before business checks."""
    t = type(value)
    if t in (dict,list):
        depth += 1
        if depth > 32: _fail(path,'container depth exceeds 32')
        if active is None: active=set()
        marker=id(value)
        if marker in active: _fail(path,'cyclic value')
        active.add(marker)
        try:
            if t is dict:
                for key,item in value.items():
                    _json_string(key,path+'.<key>')
                    _primitive(item,path+'.'+key,depth,active)
            else:
                if len(value)>10000:_fail(path,'array exceeds 10000')
                for i,item in enumerate(value):_primitive(item,f'{path}[{i}]',depth,active)
        finally: active.remove(marker)
    elif t is str: _json_string(value,path)
    elif t is int: _scalar('I',value,path)
    elif t not in (bool,type(None)): _fail(path,'not a strict JSON primitive')


def _json_string(value, path):
    if type(value) is not str or len(value)>4096:_fail(path,'expected JSON string <=4096')
    if any(0xD800<=ord(c)<=0xDFFF for c in value):_fail(path,'surrogate code point')


def _scalar(spec, value, path):
    if spec == 'S':
        _json_string(value,path)
        if not value or value==_UNKNOWN:_fail(path,'expected nonempty known string')
    elif spec == 'I':
        if type(value) is not int or not 0<=value<=2**63-1:_fail(path,'expected bounded exact integer')
    elif spec == 'B':
        if type(value) is not bool:_fail(path,'expected exact boolean')
    elif spec in ('D','SD','P','Q'):
        if type(value) is not str or not _DECIMAL.fullmatch(value) or value=='-0':_fail(path,'noncanonical decimal')
        if spec != 'SD' and value.startswith('-'):_fail(path,'negative decimal forbidden')
        fractional=len(value.split('.')[1]) if '.' in value else 0
        digits=value.lstrip('-').replace('.','').lstrip('0')
        if fractional>18 or len(digits)>38:_fail(path,'decimal precision bound')
        d=_decimal.Decimal(value)
        if spec in ('P','Q') and d<=0:_fail(path,'expected positive decimal')
        if spec=='Q' and (d.as_tuple().digits.count(1)!=1 or any(x not in (0,1) for x in d.as_tuple().digits)):_fail(path,'quantum must be power of ten')
    elif spec == 'H':
        if type(value) is not str or not _re.fullmatch(r'[0-9a-f]{64}',value):_fail(path,'expected lowercase SHA256')
    elif spec == 'UUID':
        if type(value) is not str or not _re.fullmatch(r'[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}',value):_fail(path,'expected UUID4 hex')
    elif spec == 'R':
        if type(value) is not str or not _re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',value):_fail(path,'invalid stable reason')
    elif spec == 'T':
        if type(value) is not str or not _TIME.fullmatch(value):_fail(path,'expected UTC RFC3339 Z')
        try:_datetime.datetime.fromisoformat(value[:-1]+'+00:00')
        except ValueError:_fail(path,'invalid calendar time')
    elif spec != 'JSON':_fail(path,'unknown scalar schema')


def _object(schema, value, path):
    if type(value) is not dict:_fail(path,'expected exact object')
    if value.keys()!=schema.keys():
        _fail(path,f'fields missing={sorted(schema.keys()-value.keys())} extra={sorted(value.keys()-schema.keys())}')
    for key,spec in schema.items():
        if spec=='ObservationPayload':_object(_OBS_PAYLOADS.get(value['operation'], ({},()))[0],value[key],path+'.payload')
        elif spec=='StorePayload':
            if value['event_type'] not in _STORE_PAYLOADS:_fail(path,'unsupported store event')
            _object(_STORE_PAYLOADS[value['event_type']],value[key],path+'.payload')
        else:_check(spec,value[key],path+'.'+key)


def _check(spec,value,path):
    if type(spec) is tuple:
        tag=spec[0]
        if tag=='unknown' and type(value) is str and value==_UNKNOWN:return
        if tag=='nullable' and value is None:return
        if tag in ('unknown','nullable'):_check(spec[1],value,path)
        elif tag=='enum':
            if type(value) is not str or value not in spec[1]:_fail(path,'unsupported enum')
        elif tag=='literal':
            if type(value) is not type(spec[1]) or value!=spec[1]:_fail(path,'invalid literal')
        elif tag=='list':
            if type(value) is not list:_fail(path,'expected JSON list')
            for i,item in enumerate(value):_check(spec[1],item,f'{path}[{i}]')
            if spec[2] and len({_dump(x) for x in value})!=len(value):_fail(path,'duplicate array item')
        return
    if type(spec) is dict:_object(spec,value,path);return
    name=spec
    if name in ('proposal','permit','receipt'):
        discriminator='receipt_type' if name=='receipt' else 'action'
        if type(value) is not dict:_fail(path,'expected tagged object')
        tag=value.get(discriminator)
        if type(tag) is not str or name+':'+tag not in _SCHEMAS:_fail(path,'invalid discriminator')
        name=name+':'+tag
    if name in _SCHEMAS:
        _object(_SCHEMAS[name],value,path)
        _invariants(name,value,path)
    else:_scalar(name,value,path)


def _dump(value):
    return _json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def _digest(value):return _hashlib.sha256(_dump(value).encode('utf-8')).hexdigest()
def _without(value,key):return {k:v for k,v in value.items() if k!=key}
def _dec(value):return _decimal.Decimal(value)
def _known(*values):return all(v!=_UNKNOWN for v in values)
def _equal(a,b,path,label):
    if a!=b:_fail(path,label)
def _unique(rows,key,path):
    vals=[row[key] for row in rows]
    if len(set(vals))!=len(vals):_fail(path,'duplicate '+key)
def _ordered(v,a,b,path):
    if _datetime.datetime.fromisoformat(v[b][:-1]+'+00:00')<=_datetime.datetime.fromisoformat(v[a][:-1]+'+00:00'):_fail(path,'expiry must follow issuance')
def _no_user_reserved(v,path):
    for key in ('command_id','target_command_id'):
        if key in v and type(v[key]) is str and v[key].startswith('__'):_fail(path,'reserved command namespace')
def _contains_unknown(v):
    if type(v) is dict:return any(_contains_unknown(x) for x in v.values())
    if type(v) is list:return any(_contains_unknown(x) for x in v)
    return v==_UNKNOWN


def _invariants(name,v,path):
    # 38 coefficient digits plus 18 fraction positions and array sums fit 100.
    # The caller's Decimal context, flags and traps are never changed.
    with _decimal.localcontext() as context:
        context.prec=100
        _relations(name,v,path)


def _relations(name,v,path):
    if name.startswith('proposal:') or name=='command':
        _no_user_reserved(v,path);_ordered(v,'created_at','expires_at',path)
    if name=='command':
        _equal(v['canonical_payload_sha256'],_digest({k:v[k] for k in _PAYLOAD_KEYS}),path,'canonical payload hash mismatch')
        if _known(v['chain_id'],v['collateral_asset'],v['unit_scale']):
            _equal(v['asset_key'],dict(chain_id=v['chain_id'],asset_id=v['collateral_asset'],unit_scale=v['unit_scale']),path,'cash asset mismatch')
    elif name in ('policy','approval'):
        for key in ('allowed_actions','sides','account_refs','venues','condition_ids','token_ids'):
            if key in v and any('*' in x for x in v[key]):_fail(path,'wildcard scope forbidden')
        if any(x not in ('submit','request_cancel') for x in v['allowed_actions']):_fail(path,'unsupported action scope')
        if any(x not in ('BUY','SELL') for x in v['sides']):_fail(path,'unsupported side scope')
        if name=='approval':_ordered(v,'issued_at','expires_at',path)
    elif name.startswith('permit:'):
        _no_user_reserved(v,path);_ordered(v,'issued_at','expiry',path)
        _equal(v['allowed_actions'],[v['action']],path,'permit grants different action')
        _equal(v['permit_id'],_digest(_without(v,'permit_id')),path,'permit ID hash mismatch')
        if name=='permit:submit':
            c=v['reserve_context'];p=c['proposal'];a=c['account_snapshot'];b=c['book_snapshot'];approval=c['approval'];policy=c['policy']
            if p['action']!='submit':_fail(path,'submit context requires submit proposal')
            for field,record in [('proposal_sha',p),('account_snapshot_sha',a),('book_snapshot_sha',b),('policy_sha',policy)]:_equal(v[field],_digest(record),path,field+' mismatch')
            for field,record in [('account_snapshot_sha',a),('book_snapshot_sha',b),('policy_sha',policy)]:_equal(p[field],_digest(record),path,'proposal '+field+' mismatch')
            _equal(approval['proposal_sha'],_digest(p),path,'approval proposal mismatch')
            _equal(approval['policy_sha'],_digest(policy),path,'approval policy mismatch')
            for field in ['command_id','mode',*_SCOPE,*_UNITS,*_SIZING]:_equal(v[field],p[field],path,'permit proposal '+field+' mismatch')
            for field in _SCOPE:
                _equal(v[field],a[field],path,'account scope mismatch');_equal(v[field],approval[field],path,'approval scope mismatch')
            _equal(v['mode'],a['mode'],path,'account mode mismatch');_equal(v['mode'],approval['mode'],path,'approval mode mismatch')
            for field in ['venue','condition_id','token_id',*_UNITS]:_equal(v[field],b[field],path,'book '+field+' mismatch')
            for field in _UNITS:
                _equal(v[field],a[field],path,'account units mismatch');_equal(v[field],policy[field],path,'policy units mismatch')
            for field in ('approval_receipt_ref','approval_receipt_sha'):_equal(v[field],approval[field],path,'approval evidence mismatch')
            _equal(v['expected_state_version'],c['source_global_version'],path,'context version mismatch')
            _equal(v['expected_state_sha256'],c['source_state_sha256'],path,'context state hash mismatch')
            _equal(a['state_version'],c['source_global_version'],path,'account state version mismatch')
            _equal(a['state_sha256'],c['source_state_sha256'],path,'account state hash mismatch')
            _equal(c['store_binding'],approval['store_binding'],path,'approval store binding mismatch')
            payload={k:(b['protocol_evidence_sha'] if k=='protocol_evidence_sha' else p[k]) for k in _PAYLOAD_KEYS}
            _equal(v['canonical_payload_sha256'],_digest(payload),path,'permitted payload hash mismatch')
    elif name=='ReserveGroup':
        if v['external_overlap_amount']!=_UNKNOWN and _dec(v['external_overlap_amount'])>_dec(v['local_amount']):_fail(path,'overlap exceeds local amount')
    elif name=='ReservationChange':
        if _dec(v['external_overlap_amount'])>_dec(v['amount']):_fail(path,'overlap exceeds amount')
    elif name=='account_snapshot':_account_relations(v,path)
    elif name=='book_snapshot':
        if v['complete'] and (any(v[k]==_UNKNOWN for k in [*_UNITS,'clock_skew_seconds','tick_size','share_step','min_order_size','fee_bound','fee_schedule_sha','protocol_evidence_sha','minimum_unit']) or not v['clock_verified']):_fail(path,'complete book contains unknown units/market/clock')
    elif name=='OrderRow':
        _equal(_dec(v['original_shares']),_dec(v['cumulative_fill_shares'])+_dec(v['remaining_shares']),path,'order shares identity')
    elif name=='FillRow':
        if v['fee_status']=='KNOWN' and any(v[k] is None for k in ('fee_amount','fee_asset','fee_unit')):_fail(path,'known fee missing amount or units')
        if v['fee_status']=='UNKNOWN' and v['fee_amount'] is not None:_fail(path,'unknown fee must use null amount')
    elif name=='observation':
        if v['status'] not in _OBS_PAYLOADS[v['operation']][1]:_fail(path,'status not valid for operation')
        c=v['coverage']
        if v['complete'] and (c['has_more'] or c['to_cursor'] is not None or (c['scope']=='account_all' and not c['account_scope_verified'])):_fail(path,'incomplete observation coverage')
        if not v['complete'] and not v['problems']:_fail(path,'incomplete observation needs problem')
        if c['has_more'] and c['to_cursor'] is None:_fail(path,'pagination requires next cursor')
        if v['source_timestamp'] is None and 'SOURCE_TIME_UNKNOWN' not in v['problems']:_fail(path,'null source time needs reason')
    elif name=='account_event':
        if (v['fill'] is not None)!=(v['event_type']=='FILL'):_fail(path,'fill discriminator mismatch')
        if v['event_type']=='FEE' and v['fee'] is None:_fail(path,'fee event requires fee evidence')
        if v['event_type']=='RESERVATION_UPDATE' and v['reservation_change'] is None:_fail(path,'reservation event missing change')
        if (v['reverses_event_id'] is not None)!=(v['event_type']=='REVERSAL'):_fail(path,'reversal discriminator mismatch')
    elif name=='settlement_evidence':
        _equal(v['evidence_sha256'],_digest(_without(v,'evidence_sha256')),path,'settlement self hash mismatch')
        if _known(v['chain_id']):_equal(v['chain_id'],v['asset']['chain_id'],path,'settlement chain mismatch')
        if _known(v['shares_redeemed'],v['token_balance_before'],v['token_balance_after']):_equal(_dec(v['token_balance_before'])-_dec(v['token_balance_after']),_dec(v['shares_redeemed']),path,'redeemed token identity')
        if _known(v['net_collateral_credit'],v['collateral_before'],v['collateral_after']):_equal(_dec(v['collateral_after'])-_dec(v['collateral_before']),_dec(v['net_collateral_credit']),path,'net collateral identity')
        if _known(v['shares_redeemed'],v['payout_per_share'],v['gross_collateral_credit']):_equal(_dec(v['shares_redeemed'])*_dec(v['payout_per_share']),_dec(v['gross_collateral_credit']),path,'gross collateral identity')
    elif name.startswith('receipt:'):_receipt_relations(name,v,path)
    elif name=='CommandSummary':
        _no_user_reserved(v,path)
        if v['cancel_confirmed'] and not v['cancel_requested']:_fail(path,'cancel confirmation without request')
    elif name=='CommandAccounting':
        for flag,amount in [('cost_complete','actual_cost'),('fees_complete','actual_fee'),('cash_complete','cash_delta')]:
            if v[flag] and v[amount]==_UNKNOWN:_fail(path,'complete accounting cannot hide unknown')
        if any(v[k]==_UNKNOWN for k in ('actual_cost','actual_fee','cash_delta')) and not v['unknowns']:_fail(path,'unknown accounting needs reason')
    elif name=='CommandState':
        if v['permit']['action']!='submit':_fail(path,'command state requires submit permit')
        _equal(v['record']['permit_id'],v['permit']['permit_id'],path,'command permit mismatch')
        _equal(v['record']['command_id'],v['permit']['command_id'],path,'command ID mismatch')
        _equal(v['record']['canonical_payload_sha256'],v['permit']['canonical_payload_sha256'],path,'command payload mismatch')
        _equal(_dec(v['cumulative_fill_shares'])+_dec(v['remaining_shares']),_dec(v['record']['shares']),path,'state shares identity')
        if v['attempt_count']>1:_fail(path,'more than one attempt')
    elif name=='store_event':
        global_event=v['event_type'] in ('STOP_SET','ACCOUNT_BASELINED','ACCOUNT_RECONCILED','APPROVAL_REVOKED')
        if global_event!=(v['command_id']=='__store__'):_fail(path,'global event command ID mismatch')
        if not global_event:_no_user_reserved(v,path)
        _equal(v['payload_sha256'],_digest(v['payload']),path,'payload hash mismatch')
        p=v['payload']
        if v['event_type']=='CANCEL_REQUESTED' and p['permit']['action']!='request_cancel':_fail(path,'cancel event requires cancel permit')
        if v['event_type']=='RESERVE_ACCEPTED' and p['permit']['action']!='submit':_fail(path,'reserve requires submit permit')
        if v['event_type']=='ACCOUNT_RECONCILED' and p['reconciliation']['receipt_type']!='reconciliation':_fail(path,'reconciliation receipt subtype mismatch')
        if v['event_type']=='CANCEL_OBSERVED' and p['observation']['operation']!='cancel':_fail(path,'cancel observation mismatch')
    elif name=='store_snapshot':
        _equal(v['store_id'],v['store_binding']['store_id'],path,'store ID binding mismatch')
        ids=[c['record']['command_id'] for c in v['commands']]
        if len(ids)!=len(set(ids)):_fail(path,'duplicate stored command')
        _unique(v['reservations'],'reservation_id',path);_unique(v['event_refs'],'event_id',path)
        if v['account']!=_UNKNOWN:
            for c in v['commands']:
                for field in ('account_ref','mode'):_equal(c['record'][field],v['account'][field],path,'cross-account/mode command')
                _equal(c['permit']['reserve_context']['store_binding'],v['store_binding'],path,'stored command binding mismatch')
        for ref in v['event_refs']:
            if ref['version']>v['version']:_fail(path,'event beyond snapshot head')
    elif name=='controller_result':
        if v['command_id'] is not None:_no_user_reserved(v,path)


def _account_relations(v,path):
    cash='cash_total cash_external_available cash_external_reserved cash_local_additional_reserved cash_reserved cash_available'.split()
    groups=v['reserve_groups'];complete=v['status']=='COMPLETE'
    if complete:
        if not v['clock_verified'] or v['clock_skew_seconds']==_UNKNOWN:_fail(path,'complete account needs known clock')
        if any(v[k]==_UNKNOWN for k in [*cash,*_UNITS]):_fail(path,'complete account contains unknown cash/units')
        if not all(v['coverage'][k] for k in 'cash_complete positions_complete open_orders_complete fills_complete funding_complete fees_complete pagination_complete cursors_contiguous'.split()) or v['coverage']['next_cursor'] is not None or v['coverage']['missing_ranges']:_fail(path,'complete account lacks coverage')
    if not complete and not v['problems']:_fail(path,'HOLD account needs stable problem')
    for key in cash:
        if v[key]!=_UNKNOWN and _dec(v[key])<0 and complete:_fail(path,'negative COMPLETE cash')
    for p in v['positions']:
        if complete and any(_dec(p[k])<0 for k in 'settled_shares pending_in_shares pending_out_shares available_shares reserved_shares'.split()):_fail(path,'negative COMPLETE position')
    if _known(v['cash_total'],v['cash_external_available'],v['cash_external_reserved']):_equal(_dec(v['cash_total']),_dec(v['cash_external_available'])+_dec(v['cash_external_reserved']),path,'cash total identity')
    if _known(v['cash_reserved'],v['cash_external_reserved'],v['cash_local_additional_reserved']):_equal(_dec(v['cash_reserved']),_dec(v['cash_external_reserved'])+_dec(v['cash_local_additional_reserved']),path,'cash reserved identity')
    if _known(v['cash_available'],v['cash_total'],v['cash_reserved']):_equal(_dec(v['cash_available']),_dec(v['cash_total'])-_dec(v['cash_reserved']),path,'cash available identity')
    external=[]
    for g in groups:
        external.extend(g['external_reservation_ids'])
        if _known(v['chain_id'],v['collateral_asset'],v['unit_scale']):_equal(g['asset_key'],dict(chain_id=v['chain_id'],asset_id=v['collateral_asset'],unit_scale=v['unit_scale']),path,'reserve asset mismatch')
        if complete and (g['external_overlap_amount']==_UNKNOWN or (g['external_reservation_ids'] and g['mapping_evidence_ref']==_UNKNOWN)):_fail(path,'unknown reserve overlap requires HOLD')
    if len(external)!=len(set(external)):_fail(path,'external reserve used twice')
    _unique(groups,'reservation_id',path)
    if v['cash_local_additional_reserved']!=_UNKNOWN and all(g['external_overlap_amount']!=_UNKNOWN for g in groups):
        _equal(_dec(v['cash_local_additional_reserved']),sum((_dec(g['local_amount'])-_dec(g['external_overlap_amount']) for g in groups),_decimal.Decimal(0)),path,'additional reserve identity')
    _equal(v['state_version'],v['projection']['source_version'],path,'projection source version mismatch')
    _equal(v['state_sha256'],v['projection']['source_sha256'],path,'projection source hash mismatch')
    _unique(v['approval_usage'],'approval_receipt_sha',path)
    _unique(v['known_order_refs'],'command_id',path)


def _receipt_relations(name,v,path):
    _equal(v['receipt_id'],_digest(_without(v,'receipt_id')),path,'receipt ID hash mismatch')
    if name=='receipt:reconciliation':
        if v['verdict'] not in ('MATCH','HOLD'):_fail(path,'invalid reconciliation verdict')
        if v['verdict']=='MATCH' and any(x!='MATCH' for x in v['checked_dimensions'].values()):_fail(path,'MATCH with unchecked dimension')
    elif name=='receipt:settlement_validation':
        if v['verdict'] not in ('VERIFIED','PENDING','HOLD'):_fail(path,'invalid settlement verdict')
        if v['verdict']=='VERIFIED' and (not v['cash_credit_verified'] or v['verified_net_collateral_credit']==_UNKNOWN or v['finality']!='FINALIZED' or v['settlement_stage']!='CASH_FINAL' or _contains_unknown(v['fee_totals_by_asset']) or v['unknowns'] or v['problems']):_fail(path,'verified settlement hides unknown/finality')
    else:
        if v['verdict'] not in ('COMPLETE','UNRESOLVED'):_fail(path,'invalid closeout verdict')
        summaries=v['command_summaries'];ids=[c['command_id'] for c in summaries]
        if ids!=sorted(set(ids)):_fail(path,'summaries must be unique and sorted')
        if any(c['mode']!=v['mode'] for c in summaries):_fail(path,'cross-mode summary')
        requested=[c for c in summaries if c['cancel_requested']]
        _equal(v['cancel_requested'],bool(requested),path,'cancel request aggregate mismatch')
        _equal(v['cancel_confirmed'],bool(requested) and all(c['cancel_confirmed'] for c in requested),path,'cancel confirmation aggregate mismatch')
        unknown=any(v[k]==_UNKNOWN for k in ('actual_cost','actual_fee','cash_delta')) or any(_contains_unknown(c) for c in summaries)
        if unknown and not v['unknowns']:_fail(path,'unknown closeout must disclose unknowns')
        if v['verdict']=='COMPLETE' and (unknown or v['unknowns'] or v['problems'] or v['reconcile_verdict']!='MATCH' or not v['cash_reconciled']):_fail(path,'complete closeout hides uncertainty')
        if v['coverage']=='FULL' and (v['stage']!='S1' or v['evidence_class']!='EXTERNAL' or v['unverified_capabilities'] or 'final_cash_settlement' not in v['tested_capabilities']):_fail(path,'FULL requires S1 external final settlement')
        for key in ('actual_cost','actual_fee'):
            if v[key]!=_UNKNOWN and all(c[key]!=_UNKNOWN for c in summaries):_equal(_dec(v[key]),sum((_dec(c[key]) for c in summaries),_decimal.Decimal(0)),path,'additive '+key+' mismatch')


def validate_record(kind: str, value: Record) -> Record:
    """Return a deep validated copy of one exact supported record kind."""
    if type(kind) is not str or kind not in _KINDS:_fail('$','unsupported record kind')
    if type(value) is not dict:_fail('$','record must be an exact dictionary')
    _primitive(value)
    _check(kind,value,'$')
    return _copy.deepcopy(value)


def canonical_sha256(value: Record) -> str:
    """Hash every supplied JSON member; never infer kind or omit self-IDs."""
    if type(value) is not dict:_fail('$','hash root must be an exact dictionary')
    _primitive(value)
    return _digest(value)


def decode_record(kind: str, raw: bytes) -> Record:
    """Decode bounded UTF-8 JSON, rejecting duplicates before dictionary loss."""
    if type(raw) is not bytes or len(raw)>1048576:_fail('$','expected bytes <=1MiB')
    if raw.startswith(b'\xef\xbb\xbf'):_fail('$','UTF-8 BOM forbidden')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:_fail('$','duplicate JSON key')
            result[key]=value
        return result
    def forbidden(token):_fail('$','float/nonfinite JSON token forbidden')
    try:
        value=_json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_float=forbidden,parse_constant=forbidden)
    except (UnicodeError,ValueError,RecursionError) as exc:
        if isinstance(exc,ValidationError):raise
        raise ValidationError('invalid JSON/UTF-8') from exc
    return validate_record(kind,value)
