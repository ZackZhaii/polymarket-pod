# Operator checklist

Paper loop only. Do these yourself. Do not paste keys into any chat.
`config/risk.toml` stays `mode = "paper"`, `live_enabled = false`, `require_human_gate = true`.

Cloud Grok, local Grok Build, and Grok Bot meet only through this git repo.
No marketplace plugin is required for that.

## Do now

1. Merge Pull Request #1 after reading the diff.
   https://github.com/ZackZhaii/polymarket-pod/pull/1
   Then `git pull` in the local checkout.

2. Local Grok Build, on the same machine as the checkout.
   Install, then sign in. Do not put an API key in the repo.
   Linux or macOS: `curl -fsSL https://x.ai/cli/install.sh | bash`
   Windows: `irm https://x.ai/cli/install.ps1 | iex`
   Then `grok login` and `cd` into the checkout (`~/projects/polymarket-pod`,
   Osaka VPS `~/polymarket-pod`, or `D:\projects\polymarket-pod`).
   Check with `grok inspect` and `grok -p "Read docs/LOOP.md and stop. Do not edit."`

3. GitHub on that same machine, as ZackZhaii, for this private repo.
   `gh auth login` then `git status` and `git pull`.
   Do not paste the token into Grok, Grok Bot, or `.env`.

4. Grok Bot on that same machine, with Shell, directory limited to this checkout.
   Paste the block in `docs/GROK_LINK.md`. One task, then stop.
   The bot runs local `grok -p` itself. Do not install a third-party relay.

5. Cloud side is already on.
   GitHub is connected in Grok. Automation `polymarket-pod-paper-loop`
   runs daily at 10:30 Asia/Tokyo. It opens at most one pull request
   and does not merge it. Nothing else to connect.

## Do not install

Phase 0 does not need a plugin, skill pack, or MCP server.

- Do not install marketplace plugins for this repo
  (MongoDB, Vercel, Sentry, Chrome DevTools, Cloudflare, Superpowers).
- Do not install `polymarket-client`, `py-sdk`, or any order-signing client.
- Do not add a trading MCP. `docs/SOURCES.md` keeps trading MCP in preview
  until a later human gate. Read-only Gamma and CLOB `/book` already use
  the Python standard library.
- Do not create `.env`. Do not fill `POLY_PRIVATE_KEY` or API secrets.
- Do not add Telegram, a withdraw path, or geo-bypass steps.
- Do not install https://github.com/sec-knight/grokbot-build-relay
  unless you have read the scripts. It is not an xAI product.
  `grok -p` from Grok Bot is enough.

## After you finish

Reply with the item numbers you completed.
The next code task is still whatever `docs/LOOP.md` marks as not done.
If every task is done, do not invent a live client.
