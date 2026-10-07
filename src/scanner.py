"""Phase 0 read-only Gamma scanner. No keys. No orders."""
from __future__ import annotations

import json
import urllib.request

GAMMA = "https://gamma-api.polymarket.com/markets?closed=false&limit=20"


def fetch_markets() -> list[dict]:
    req = urllib.request.Request(GAMMA, headers={"User-Agent": "polymarket-pod-phase0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    return data if isinstance(data, list) else data.get("data", [])


def main() -> None:
    markets = fetch_markets()
    print(f"active_sample={len(markets)} mode=paper keys=none")
    for m in markets[:10]:
        print("-", m.get("question") or m.get("slug") or m.get("id"))


if __name__ == "__main__":
    main()
