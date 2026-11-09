"""Wire one real returned observation to an independently owned receipt file.

Importing this module loads the existing public API code, not application
inputs. No runtime, schedule, clock, parser, or third file format is added.
"""

from .m2_observe_slot import observe_slot_once
from .m2_receipt import persist_observation_receipt


def observe_persisted_once(planned_slot, *, attempt_id, spec, archive_owned_root,
                           archive_file_name, receipt_owned_root, receipt_file_name):
    """Call real EY once; only its actual returned value goes to real EZ once.

    EY validates its original plan/attempt/spec/archive arguments. EZ validates
    receipt arguments after EY may already have read source or created raw;
    this is not atomic validation of all inputs before the first effect.
    The caller must own the explicitly supplied stable local synthetic source
    and separate archive/result roots. Parameters confer no real IO authority.

    Return exactly schema/status/declaration_status/metrics/denominator/
    healthy_online_us/observation/receipt_write: original EY13 and EZ18 objects
    stay separate. Real EZ file7 preserves this observation snapshot; returned
    failures/REFUSED/UNKNOWN are not promoted, repaired, or reinterpreted.

    Any exception/resource/Base escaping EY propagates without an EZ call;
    EY's internally converted CAPTURE_ERROR remains a returned observation.
    Any escaping EZ exception propagates, without rollback of raw or partial
    effects and without a forged receipt. Interrupts/no EY return/writer failure
    may leave no result file. Existing cleanup-Base limitations remain.

    Only the existing EV clocks/source/protocol-code closure and EW/EZ owned
    file IO occur. Supplied planned time, EV endpoints and producer declaration
    time remain distinct; no new clock/source reread/GET/lock/loop/CLI/hook/WAL.
    WRITTEN_READ_BACK proves only the selected result bytes synchronously read
    back, not old archive authenticity, power-loss durability, identity, health,
    PIT/M2/profit or complete failure audit. Import code IO is not application IO.
    """
    observation = observe_slot_once(
        planned_slot, attempt_id=attempt_id, spec=spec,
        owned_root=archive_owned_root, file_name=archive_file_name,
    )
    receipt_write = persist_observation_receipt(
        observation, owned_root=receipt_owned_root, file_name=receipt_file_name,
    )
    return {"schema": "m2_observe_persisted/1", "status": "NOT_EVALUATED",
            "declaration_status": "UNVERIFIED", "metrics": None,
            "denominator": None, "healthy_online_us": None,
            "observation": observation, "receipt_write": receipt_write}
