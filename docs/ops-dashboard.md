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
even by accident); the one thing it does over the network is resolve
display names via Discord's REST API, reusing the bot's own
`DISCORD_BOT_TOKEN` (see "Display names" below) - `127.0.0.1` is still
deliberate, though: this dashboard has **no login of its own** (it was
built for an audience of one - see `web/README.md`), so whatever can reach
the port can see every server's balances and player names with nothing
else standing in the way.

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

## Display names

`dragonhoard.db` only ever stores Discord's numeric IDs - `server_config`
has no guild name column, `users` has no username column, and there's no
channel-name column either. `web/discord_lookup.py` resolves real names for
them via Discord's REST API (the bot's own `DISCORD_BOT_TOKEN`, already in
your `.env` - no gateway connection, no new credential), cached in memory
for an hour so it isn't refetching on every page load. The first load after
a (re)start is the only slow one, while that first batch resolves.

A guild the bot has been removed from, or a deleted account, can't be
looked up this way and falls back to a truncated-ID label (`Server •0121`,
`user_2096`) - and you can override any name, looked-up or not, with
`web/directory.json` (gitignored - personal to your servers, not secret;
template at `web/directory.example.json`):

```json
{
  "guilds": {"1039472118835200121": "Dragon's Den"},
  "users": {"284119203847172096": "greendragon888"},
  "channels": {"1039472118835200122": "#dragonhoard"}
}
```

An entry there always wins over Discord. The file is re-read on every
request, so editing it doesn't need a restart.

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

Beta's own working unit file lives at the repo root -
`dragonhoard-beta-web.service` (mirrors `dragonhoard-beta.service`: runs as
`isaac`, no dependency on the bot's own service since the dashboard only
needs the `.db` file to exist). Install and enable it once, after `pip
install -r requirements-web.txt` in this checkout's venv:

```bash
cp dragonhoard-beta-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dragonhoard-beta-web
```

Production's counterpart is **not** shipped as a file here - `/opt/dragonhoard`
is never edited directly (see its own `CLAUDE.md`), so this only ever gets
created by hand there, the same way `dragonhoard.service` originally was.
Once `web/` has been merged to `main` and pulled into `/opt/dragonhoard`
(`update.sh`), and `venv/bin/pip install -r requirements-web.txt` has run
there too:

```ini
[Unit]
Description=Dragonhoard Ops dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dragonbot
WorkingDirectory=/opt/dragonhoard
# Same host as beta's - this is one machine, two checkouts - on the next
# port up so the two don't collide.
ExecStart=/opt/dragonhoard/venv/bin/uvicorn web.app:app --host 100.70.34.72 --port 8421
Restart=on-failure
RestartSec=10

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Save as `/etc/systemd/system/dragonhoard-web.service`, then
`systemctl daemon-reload && systemctl enable --now dragonhoard-web` -
deliberately not done as part of this change, since the branch it depends
on hadn't been merged yet when this was written.

Save as `/etc/systemd/system/dragonhoard-web.service`, then
`systemctl daemon-reload && systemctl enable --now dragonhoard-web`.
