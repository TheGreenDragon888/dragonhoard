# Dragonhoard Ops

A private, read-only developer dashboard over `dragonhoard.db` - scale,
currency health, production queues, mining pools, wealth distribution, and
alerts across every server the bot is running in. Built from a Claude
Design mockup (`Dragonhoard Ops`) against the real schema; see
`docs/ops-dashboard.md` at the repo root for how to run it, `directory.json`
setup, and what's approximated vs. exact.

Not for players or server managers - nothing here is gated by Discord
permissions, and every currency figure is deliberately per-server (never
summed across servers, since they aren't the same money).

- `queries.py` - all derivation, importing `config.py`/`data/materials.py`
  directly so machine levels, mining slots, and pool math can never drift
  from what the bot itself computes.
- `app.py` - the one `/api/ops` endpoint (query-string settings: anonymize,
  hideDeparted, dormantDays, burnFloor, stalledDays) plus static serving.
- `directory.py` - the guild/user/channel display-name lookup (the database
  only stores Discord snowflakes): `directory.json` overrides, then
  `discord_lookup.py`, then a truncated-ID label as the last resort.
- `discord_lookup.py` - resolves real names via Discord's REST API (the
  bot's own token, no gateway, cached in memory).
- `static/` - the frontend: `index.html` + `app.js` (vanilla, no build
  step) + `styles.css` (vendored Isaac Day Design System tokens/components).
