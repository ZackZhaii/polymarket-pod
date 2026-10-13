"""Issue #268 (Task DL): ``os.replace`` with a bounded retry of a Windows ``PermissionError``. Standard library only.

Windows refuses ``os.replace`` with ``PermissionError`` while another handle briefly holds the target:
a reader opened without FILE_SHARE_DELETE (Python's own ``open`` is one), an antivirus or indexer scan.
POSIX never does, so there the error is real and is raised at once.

``replace_with_retry`` tries at most ``REPLACE_ATTEMPTS`` times in all (at least once), sleeping
``REPLACE_BACKOFF_S * 2**n`` after the n-th failed try: 0.02 + 0.04 + 0.08 + 0.16 = 0.30 s worst case.
Only a ``PermissionError`` on Windows is retried. Any other exception, and every exception on another
platform, propagates at once, exactly as ``os.replace`` raises it. When the tries run out the last
``PermissionError`` is re-raised unchanged (never swallowed, never wrapped), so the caller fails as it
always did and removes its temp file (fail closed). Operational limits, not risk thresholds; nothing
here is read from config/risk.toml. Tests monkeypatch the constants, ``_sleep`` and ``_retry_platform``.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPLACE_ATTEMPTS = 5
REPLACE_BACKOFF_S = 0.02
_sleep = time.sleep  # tests record instead of sleeping


def _retry_platform() -> bool:
    """True on Windows, the only platform where a held target makes os.replace raise PermissionError."""
    return os.name == "nt" or sys.platform == "win32"


def replace_with_retry(src: Path | str, dst: Path | str) -> None:
    """``os.replace(src, dst)``; on Windows only, a ``PermissionError`` is retried, bounded (see module doc)."""
    attempts = max(1, int(REPLACE_ATTEMPTS))
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt + 1 >= attempts or not _retry_platform():
                raise
        _sleep(REPLACE_BACKOFF_S * 2 ** attempt)
