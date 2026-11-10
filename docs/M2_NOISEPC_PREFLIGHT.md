# M2 #122 前置：NoisePC 只读预检（2026-11-10）

Readonly preflight of NoisePC (machineId `8b6b6865-1830-4ef9-bc33-885cfc51dd5a`) for issue #122, taken 2026-11-10 12:13–12:17 JST (Asia/Tokyo, UTC+9). No runner was started, no scheduled task was changed, the host clock was not adjusted, nothing was installed, and `config/risk.toml` was not edited. This note is observations only; it does not approve the 7-day paper run (still pod-key).

## Clock

Windows Time was synced to `ntp.aliyun.com` (stratum 4, source IP 203.107.6.88), with `time.windows.com` also listed as a peer. Last successful sync was 2026-11-10 12:13:05 JST.

| Measurement | Value | Note |
| --- | --- | --- |
| `w32tm` phase offset | 0.028669 s | Verbose status, ~162 s after last sync |
| Stripchart vs `ntp.aliyun.com` (5 samples) | +0.0069 s to +0.0206 s | Query only; local clock slightly behind that NTP server |
| Root delay | 0.0685 s | |
| Root dispersion | 7.7989 s | Large uncertainty flag; the measured offset above is much smaller |
| Public CLOB `GET /time` | Body `1794280549` (= 2026-11-10 03:15:49 UTC) | Integer seconds, so it cannot be compared to the 0.1 s limit |
| One public CLOB `GET /book` | `lead_s = book_ts − local receive = −4.591` s | Same definition as `m1_run.clock_check`; `ok` vs `M1_MAX_CLOCK_BEHIND_S = 0.1`. A negative lead only shows the book was last updated earlier; it is a lower bound on lag, not a tight now-vs-exchange sample |

`w32tm /query /source` returned access denied (0x80070005); status and peers were still readable. The clock was not resynced.

## Disk

| Volume | Size | Free | Free ≥ 2 GB (M2 pause line in #122) |
| --- | --- | --- | --- |
| C: (NTFS) | 952.67 GB | 423.70 GB | yes |
| D: (NTFS) | 953.85 GB | 293.10 GB | yes |

`C:\poly\pod` and `D:\projects\polymarket-pod\runs\m2` do not exist, so the 5 GB `runs/m2` budget does not yet apply. `runs/m2-chain.jsonl` is also absent (expected before the first M2 start).

## Python environment

Default `python` on PATH is **3.14.3** (`C:\Python314\python.exe`), not a venv: `websockets` 15.0.1, `pytest` 9.0.3, `tomllib` present. That `websockets` version does not match the optional pin in `requirements-ws.txt` (`websockets==17.2.0`).

Python **3.12.10** is also installed at `C:\Users\Noise\AppData\Local\Programs\Python\Python312\python.exe`. That interpreter has no `websockets` and no `pytest`.

The M1 acceptance clone still has `D:\AI_Workspace\pod-m1-20261011T0313Z\venv312`: Python 3.12.10, `websockets` 17.2, `pytest` 8.3.5 — the versions recorded in the PASS run manifest. Nothing was installed or upgraded in this preflight.

`D:\projects\polymarket-pod` (the current shared checkout) is on `main` at `3a59beebfe09dd0ed562fa7c2adba4a481cfa153`. Its `config/risk.toml` matches the LF-normalized pin `0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04`, with `live_enabled = false`. The raw SHA differs only because of CRLF checkout. This checkout was not fetched, switched, or committed.

## M1 data directory integrity

The paths in `docs/M1_ACCEPTANCE.md` (`data/m1-runs` under the current project tree) and the M2 checkout mentioned in `docs/PHASE0_RUN.md` (`C:\poly\pod`) are **missing**. M1 run directories live under older clones in `D:\AI_Workspace\` (OpenClaw trees were not opened). File sets were listed read-only; no run was started or repaired.

### Accepted 6-hour run (PASS)

`D:\AI_Workspace\pod-m1-20261011T0313Z\polymarket-pod\data\m1-runs\m1-20261011T0313Z`

- `manifest.json` `status=completed`, exit 0, `elapsed_s=21600.422` (target 21600), `started_at=2026-10-11T03:23:52.723Z`, `ended_at=2026-10-11T09:23:54.693Z`.
- Offline `report.json` verdict **PASS**, `failed=[]`, no integrity problems, no warnings. All 11 criteria passed (`run_completed`, `run_duration`, `risk_pinned`, `integrity`, `coverage`, `book_age_p99`, `reconnects`, `gap_resyncs`, `rest_matched`, `rest_top_mismatch`, `rest_unmatched`).
- Start-up `clock_check`: `ok=true`, 10/10 samples usable, `max_behind_s=-0.464`, `median_lead_s=-2.352`, limit 0.1 s.
- `git.sha=070c5172fe297e2c3692089390ec3d79c9b582cb`, `dirty=false`. Python 3.12.10, `websockets` 17.2.
- `risk.toml` pin at start and end: `0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04`, `live_enabled=false`. `orders=none`.
- Expected files present: `books/` 6 files (8,170,965 bytes), `events/` 6 (325,196), `rest/` 6 (90,329), `raw_books/` 6 (1,899,800), plus `manifest.json`, `report.json`, `report.md`. About 10.5 MB in total. 10 assets; 11,642 book rows, 4,388 events, 144 REST rows / 36 REST samples, 0 REST errors, 2 connects, one `pong_timeout` disconnect.
- Companion scan file: `...\data\markets.jsonl` (6,816,378 bytes, 6,009 lines, sha256 `9de5ac5e557942795e70fd35958031b56c247fb5d3c4ebf7bc2f0fbea8536ec3`).

### Other retained runs (complete, not the accepted result)

`D:\AI_Workspace\pod-m1\polymarket-pod\data\m1-runs\m1-20261010T1907Z` — completed 6 h, exit 0, report **FAIL** on `coverage` and `gap_resyncs`. Git `69c8f55ef381cf4b4ff95e9ae0e0ad53e214cfb3`. Same risk pin and `live_enabled=false`. Expected books/events/rest/raw_books/manifest/reports are present (~14.9 MB). A zip of this run (`m1-20261010T1907Z.zip`, 610,643 bytes, 27 entries including manifest and reports, mtime 2026-10-11 01:57:36 UTC) also sits untracked in `D:\projects\polymarket-pod` and in `D:\AI_Workspace\pod-m1`.

`D:\AI_Workspace\pod-m1.old-20261010T1907Z\polymarket-pod\data\m1-runs\m1-20261010T1056Z` — manifest still `status=running` with no `ended_at`; report FAIL (`run_completed`, `run_duration`, `risk_pinned` because end sha is missing, `integrity`, `coverage`, `book_age_p99`). Integrity note: no usable started_at/ended_at pair. Files exist on disk that are not listed in the unfinished manifest.

Shorter leftovers in the same old clone: `smoke-20261010T0655Z` and `launchtest150s-20261010T0706Z` (both completed short runs, report FAIL, as expected for a 6 h threshold). Several `*-logs` folders hold only launcher/stdout/stderr text. `b64chunks` under `pod-m1\...\m1-runs` is not a run directory (13 `c000.txt`… files, no manifest).

## What this preflight did not do

- Did not start `m1_run`, `m2_runner`, `paper_cycle`, or Sentinel.
- Did not comment on issue #366 or any other GitHub thread.
- Did not touch `D:\eta-python` or `D:\AI_Workspace\OpenClaw`.
- Did not change PolyPod scheduled tasks, Windows power/update policy, or the M1/M2 venv.

## Items for 家伟 (not decided here)

1. M2 working tree: `C:\poly\pod` and `D:\projects\polymarket-pod\data` / `runs\m2` are missing. The accepted M1 data is only under `D:\AI_Workspace\pod-m1-20261011T0313Z\...`. Where should M2 read or copy that tree?
2. Which Python to use for M2 on NoisePC: PATH default 3.14.3 (`websockets` 15.0.1) versus the existing M1 `venv312` (3.12.10, `websockets` 17.2, matching the PASS manifest and `requirements-ws.txt`)?
3. Windows root dispersion is ~7.8 s while `M1_MAX_CLOCK_BEHIND_S` is 0.1 s. Measured NTP offset was ~7–29 ms and the one `/book` lead was not behind the limit. Whether to tighten NTP (shorter poll / resync) before a long run is a host-clock decision; this preflight did not change it.
4. The 7-day paper run in #122 remains KEY: start time, scheduled tasks, power policy, and thresholds still need an explicit approval. This document is not that approval.
