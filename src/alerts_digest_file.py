"""#244-B: one authorized regular-file capture, then the real byte kernel.

No CLI, output, cursor storage, retry or runtime integration. Trusted stable
parents and the caller's permission for this selected file are prerequisites;
the metadata window is not an atomic/adversarial snapshot guarantee.
"""
from __future__ import annotations

import ntpath
from os import fstat as _fstat, lstat as _lstat, name as _os_name
from os.path import isabs as _isabs
from builtins import open as _open
import re
from stat import S_ISREG

from src.alerts_digest import digest_snapshot


_ORDINARY = (OSError, ValueError, TypeError, AttributeError, OverflowError)
_FIELDS = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
_REPARSE_POINT = 0x400
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                      | {prefix + str(i) for prefix in ("COM", "LPT") for i in range(1, 10)}
                      | {prefix + i for prefix in ("COM", "LPT") for i in ("\u00b9", "\u00b2", "\u00b3")})


class DigestFileError(ValueError):
    """Fixed non-sensitive file phase; no path or captured log text."""


def _path(value):
    if type(value) is not str or not value:
        raise DigestFileError("path")
    if any(ord(char) < 32 for char in value) or any(char in value for char in ("$", "%", "~")):
        raise DigestFileError("path")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise DigestFileError("path") from None
    if len(value) >= 2 and all(char in "/\\" for char in value[:2]):
        raise DigestFileError("path")
    if not _isabs(value):
        raise DigestFileError("path")
    drive, _ = ntpath.splitdrive(value)
    if _os_name == "nt" and not re.fullmatch(r"[A-Za-z]:", drive):
        raise DigestFileError("path")
    for i, part in enumerate(re.split(r"[/\\]", value)):
        if not part:
            continue
        if i == 0 and re.fullmatch(r"[A-Za-z]:", part):
            continue
        folded = part.casefold()
        if (part in (".", "..") or folded == ".env" or folded.startswith(".env.")
                or ":" in part or part.endswith((".", " "))
                or part.split(".", 1)[0].upper() in _RESERVED):
            raise DigestFileError("path")
    return value


def _call(phase, operation, *args):
    try:
        return operation(*args)
    except _ORDINARY:
        raise DigestFileError(phase) from None


def _stamp(info):
    try:
        values = tuple(getattr(info, field) for field in _FIELDS)
        attrs = getattr(info, "st_file_attributes", None if _os_name == "nt" else 0)
        if (any(type(value) is not int for value in values)
                or values[0] < 0 or values[1] <= 0 or values[3] < 0
                or not S_ISREG(values[2]) or type(attrs) is not int
                or attrs < 0 or attrs & _REPARSE_POINT):
            raise DigestFileError("metadata")
        return values
    except _ORDINARY:
        raise DigestFileError("metadata") from None


def digest_file_once(alerts_path: str, *,
                     cursor: dict[str, object] | None = None) -> dict[str, object]:
    """Capture one explicitly authorized local regular file, then the real kernel.

    Exact str absolute local path; raw lexical guards run before path handling.
    One lstat/open/fstat/read/fstat/lstat window and one explicit close attempt.
    Reject unknown identity/type, reparse/symlink, metadata change, replacement,
    nonbytes/short read or failed close. No retry, seek, lock, repair or write.
    Only after capture and close succeed call the accepted digest_snapshot at
    most once, returning its original eleven-key result. Kernel cursor
    validation follows capture, so bad cursor input can follow authorized I/O.

    Ordinary file failures expose only path/open/metadata/read/changed/close.
    Ordinary cleanup preserves the primary failure; primary resource/Base
    identity survives normal/ordinary close. A cleanup resource/Base itself
    takes precedence. The close-attempt flag is set before calling close; this
    does not count GC/kill-time cleanup or guarantee release after termination.

    Only a trusted fixed-version direct same-process caller observing this
    invocation return normally can adopt this result. Any escaping failure
    yields no adoptable result. No stdout/exit-status, cursor-file parsing or
    persistence, delivery, runtime-health, exactly-once, atomic-snapshot or
    hostile-TOCTOU assurance is supplied. Stable trusted parents and explicit
    selected-file read permission remain external prerequisites.
    """
    path = _path(alerts_path)
    handle = None
    failed = False
    close_attempted = False
    try:
        before = _stamp(_call("metadata", _lstat, path))
        handle = _call("open", _open, path, "rb")
        descriptor = _call("metadata", handle.fileno)
        opened = _stamp(_call("metadata", _fstat, descriptor))
        # lstat/fstat ctime semantics can differ on Windows; identity crosses
        # interfaces, full metadata is compared within each interface below.
        if opened[:2] != before[:2]:
            raise DigestFileError("changed")
        payload = _call("read", handle.read)
        if type(payload) is not bytes or len(payload) != opened[3]:
            raise DigestFileError("read")
        after = _stamp(_call("metadata", _fstat, descriptor))
        selected_after = _stamp(_call("metadata", _lstat, path))
        if after != opened or selected_after != before:
            raise DigestFileError("changed")
    except BaseException:
        failed = True
        raise
    finally:
        if handle is not None and not close_attempted:
            close_attempted = True
            try:
                handle.close()
            except (MemoryError, RecursionError):
                raise
            except Exception:
                if not failed:
                    raise DigestFileError("close") from None
    return digest_snapshot(payload, cursor=cursor)
