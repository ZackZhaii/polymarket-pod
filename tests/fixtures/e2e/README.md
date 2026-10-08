# End-to-end offline gate fixtures (Task AJ)

Fixed, sanitised recordings for `tests/test_e2e_offline.py` and the guard's `e2e` check
(`scripts/e2e_offline.py` runs the pipeline; see docs/PHASE0_RUN.md, "E2E offline gate").

| File | What it is |
|---|---|
| `scenario.json` | The frozen clock (`now`), the second Sentinel check offset, the intent order, the duplicate-replay and corrupted-ledger plans. |
| `gamma_markets.json` | Raw Gamma `/markets` rows, the shape `scanner.scan` reads (v1 `clobTokenIds`). |
| `clob_books.json` | Raw public CLOB `GET /book` bodies per token, fed to the real `book.record` through its getter hook. |
| `forecasts.json` | NewsForecaster inputs (model_prob per market and outcome) plus the per-intent fee_rate and assumed_slippage. |
| `ledger_start.jsonl` | The paper ledger start state: three fills written by the real `paper_ledger.commit_if_allowed` / `ledger_store.book` with readable fill ids. |
| `quotes_start.jsonl` | Paper maker quotes (two open, one cancelled) that Sentinel's halt cancels and the wash check reads. |
| `golden.json` | The expected output: every allow/veto with its reasons, fills, cash, positions, Sentinel halts, bus counts. |
| `.gitattributes` | `eol=lf`, so a Windows checkout replays the same ledger bytes (the harness also normalises CRLF). |

Sanitised: market ids are `e2e-*`, token ids are short synthetic numbers, condition ids are `0xe2e*`
stubs, the `/book` hash is `e2e-sanitised`, questions are synthetic. No account, wallet, key or order
data. Prices, sizes and timestamps keep the recorded shapes (string prices, millisecond `timestamp`),
including one `"NaN"` ask, which the real recorder passes through as a float NaN.

Golden updates: `python -m pytest tests/test_e2e_offline.py --update-golden` rewrites `golden.json`
and that run fails on purpose. Review `git diff tests/fixtures/e2e/golden.json`, then rerun without the
flag. Nothing else (not the guard, not a plain pytest run, no environment variable) writes it.
