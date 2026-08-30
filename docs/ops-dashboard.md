# Dragonhoard Ops (the read-only stats dashboard)

`web/` is a private, read-only developer dashboard over `dragonhoard.db`:
scale, currency health, production queues, mining pools, wealth
distribution and a handful of alerts, across every server the bot is
running in. It's for you, not for players or server managers - see
`web/README.md` for what it is and isn't, and the design rationale.

## Running it

From the repo root, through the bot's own `venv` (it imports `config.py`
and `data/materials.py` directly, so it needs the same `.env` and the same
interpreter that already has `python-dotenv`/`tzdata` installed - a bare
`uvicorn` on your `$PATH` is very likely a *different* Python, typically an
apt/system one or a `pip install --user`, that has never heard of this
project's dependencies):

```bash
venv/bin/pip install -r requirements-web.txt
venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8420
```

If you'd rather not add fastapi/uvicorn to the bot's own venv at all, make
a second one just for this and use its `pip`/`uvicorn` instead:

```bash
python3 -m venv venv-web
venv-web/bin/pip install -r requirements-web.txt
venv-web/bin/uvicorn web.app:app --host 127.0.0.1 --port 8420
```

Then open `http://127.0.0.1:8420/` in a browser. It only reads
`dragonhoard.db` (opened with SQLite's own read-only mode, so it can't write
even by accident) and never talks to Discord - `127.0.0.1` is deliberate:
this dashboard has **no login of its own** (it was built for an audience of
one - see `web/README.md`), so whatever can reach the port can see every
server's balances and player names with nothing else standing in the way.

## Reaching it from another machine

Bind to a real interface instead of `127.0.0.1`. Prefer your Tailscale
interface over the raw LAN - Tailscale's own auth is what's standing in for
the login screen this dashboard doesn't have, whereas `0.0.0.0` hands the
page to anything on the network (roommates, guests, IoT devices):

```bash
tailscale ip -4                                                # your tailnet IP
venv/bin/uvicorn web.app:app --host <that-ip> --port 8420      # tailnet-only
venv/bin/uvicorn web.app:app --host 0.0.0.0 --port 8420        # whole LAN - avoid unless you trust it
```

If `ufw` is enabled, scope the rule to the tailnet range rather than opening
the port broadly: `sudo ufw allow from 100.64.0.0/10 to any port 8420`.

## Display names: `web/directory.json`

`dragonhoard.db` only ever stores Discord's numeric IDs - `server_config`
has no guild name column, `users` has no username column, and there's no
channel-name column either. Resolving those for real means giving this
dashboard Discord API credentials of its own, which it deliberately doesn't
have (see `web/README.md`).

Instead, copy `web/directory.example.json` to `web/directory.json`
(gitignored - it's personal to your servers, not secret) and fill in the
guilds/users/channels you recognize:

```json
{
  "guilds": {"1039472118835200121": "Dragon's Den"},
  "users": {"284119203847172096": "greendragon888"},
  "channels": {"1039472118835200122": "#dragonhoard"}
}
```

Anything left out falls back to a truncated-ID label (`Server •0121`,
`user_2096`). The file is re-read on every request, so renaming something
doesn't need a restart.

## Approximate figures

Two numbers on this dashboard are **approximations**, both because getting
the real figure needs a live Discord connection this dashboard was
deliberately built without:

- **Members** (and the market "target stock" derived from it): the bot's
  own `target_stock()` scales with `human_member_count()`, a live count of
  non-bot Discord members. The dashboard instead counts distinct
  `server_currency_balances` rows for that guild - everyone who has ever
  earned or spent that server's currency. This undercounts anyone who
  joined but never traded, and is labelled "approx." wherever it's shown.
- **"Quiet for N days"**: `server_config` and `drills` carry no timestamp
  of their own, so server activity is inferred from the newest of a queued
  production job, a posted daily job, or a paid job-board completion. A
  server with none of those ever recorded shows "no recorded activity"
  rather than a fabricated day count.

## Running it permanently (systemd)

Mirrors `dragonhoard.service` (see `docs/deployment.md`) - same user,
adjacent `WorkingDirectory`:

```ini
[Unit]
Description=Dragonhoard Ops dashboard
After=network.target

[Service]
Type=simple
User=dragonbot
WorkingDirectory=/opt/dragonhoard
ExecStart=/opt/dragonhoard/venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8420
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Save as `/etc/systemd/system/dragonhoard-web.service`, then
`systemctl daemon-reload && systemctl enable --now dragonhoard-web`.
