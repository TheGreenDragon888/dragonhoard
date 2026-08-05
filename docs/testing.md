# The testing environment, and how to ship to production

Dragonhoard runs in two completely separate installations on this container.
Nothing is shared between them - not the code checkout, not the Python
packages, not the database, not the Discord application.

| | **Production** | **Beta / testing** |
|---|---|---|
| Directory | `/opt/dragonhoard` | `/opt/dragonhoard-beta` |
| Discord app | Dragonhoard | Dragonhoard Beta |
| Database | `data/dragonhoard.db` | `data/dragonhoard-beta.db` |
| systemd service | `dragonhoard` | `dragonhoard-beta` |
| Runs as user | `dragonbot` | `isaac` |
| Git branch | `main` only | wherever you're working |
| Slash commands | synced globally (slow) | synced to one test guild (instant) |
| Custom emoji | each icon's `live_id` | each icon's `beta_id` - see 1c below |
| `.env` → `BOT_ENVIRONMENT` | unset (defaults to `live`) | `beta` |

The golden rule: **you never edit files in `/opt/dragonhoard`.** That directory
is a deployment target. Its only job is to be an exact copy of what's on
GitHub's `main` branch. All editing happens in `/opt/dragonhoard-beta`.

This is what fixes the file-permission problem too. Editing as `isaac` inside
`/opt/dragonhoard` resets group ownership on files the `dragonbot` user needs
to read, which crash-loops the live service. If you only ever *pull* into that
directory, git writes the files and ownership stays correct.

## Part 1: One-time setup

Most of this is already done. What remains needs your secrets and one `sudo`.

### 1a. Fill in the beta secrets

```bash
nano /opt/dragonhoard-beta/.env
```

Two placeholders to replace:

- `DISCORD_BOT_TOKEN` - from the [Developer Portal](https://discord.com/developers/applications),
  select **Dragonhoard Beta** (not the live app) -> **Bot** -> **Reset Token**.
  While you're on that page, make sure **Server Members Intent** is toggled on,
  same as the live bot needs - the bot won't connect without it.
- `DEV_GUILD_ID` - the ID of a test Discord server. Turn on **Developer Mode**
  (User Settings -> Advanced), then right-click the server -> **Copy Server ID**.

Save with `Ctrl+O`, `Enter`, exit with `Ctrl+X`.

**Make a separate test server.** If you invite Dragonhoard Beta into the same
server as the live bot, you'll see two nearly identical `/mine` commands in the
autocomplete list and you will run the wrong one. A private server with just you
in it is the right setup.

### 1b. Install the beta service

```bash
sudo cp /opt/dragonhoard-beta/dragonhoard-beta.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start dragonhoard-beta
sudo systemctl status dragonhoard-beta
```

Note there is deliberately **no `enable`** here. `enable` means "start on boot",
which is right for the live bot but wrong for a test bot - you don't want a
half-finished experiment silently coming back up after a reboot. Start it when
you want it, stop it when you're done.

### 1c. Uploading beta copies of the game's custom emoji

Custom Discord emoji belong to the application that uploaded them.
Dragonhoard and Dragonhoard Beta are separate applications, so every
material's icon has to be uploaded to *both* and ends up with two different
numeric ids - this is why the live bot's icons don't show up when the beta
bot sends them.

Every custom emoji in the game is defined once, in `data/materials.py` (and
one place in `cogs/mining.py`), as a call to `custom_emoji("Name", live_id,
beta_id)` (see `data/emoji.py`). Right off a fresh clone, every `beta_id` is
`None`, so on beta every one of those icons renders as `❓` instead of a
real image - visible proof it hasn't been uploaded to Dragonhoard Beta yet,
rather than a silent wrong image.

To fix one:

1. Save the icon (it's already been uploaded to the live "Dragonhoard"
   application, so grab a copy from there - Developer Portal -> Dragonhoard
   -> Emoji).
2. Upload it to **Dragonhoard Beta** instead (Developer Portal -> Dragonhoard
   Beta -> Emoji -> Upload Emoji). Give it the same name as the live one for
   readability, though Discord only actually needs the id.
3. Copy the new emoji's id (right-click it in Discord once it's usable
   somewhere, or read it back from the Developer Portal) and fill it into
   that item's `beta_id` in `data/materials.py`.
4. Restart the beta bot and check the icon renders where you'd expect.

`config.IS_BETA` (from `BOT_ENVIRONMENT` in `.env`) is what picks `beta_id`
over `live_id` - that's also how the bot tells which of the two applications
it's running as everywhere else this matters.

## Part 2: The day-to-day loop

### Write and test

```bash
cd /opt/dragonhoard-beta
git checkout -b mining-rework      # a branch named for what you're doing
nano cogs/mining.py                # ... make your changes ...
sudo systemctl restart dragonhoard-beta
journalctl -u dragonhoard-beta -f  # watch it boot; Ctrl+C stops watching
```

Then go poke at it in your test server. Because `DEV_GUILD_ID` is set, any new
or renamed slash command shows up the moment the bot finishes booting.

Run the test suite too, before you even start the bot - it catches the dumb
mistakes in two seconds instead of two minutes:

```bash
cd /opt/dragonhoard-beta && venv/bin/python -m pytest tests/ -q
```

### Ship it

When it works, from `/opt/dragonhoard-beta`:

```bash
git add -A
git commit -m "Rework mining pool top-up rates"
git checkout main
git merge mining-rework
git push origin main
git branch -d mining-rework        # tidy up the finished branch
```

### Update production

Now, and only now, touch the live bot:

```bash
/opt/dragonhoard/update.sh
```

That script pulls `main`, installs any new dependencies, restarts the service,
and shows you the status. It's the whole deploy. See Part 3 for what it does
step by step and how to undo it.

## Part 3: What `update.sh` actually does

Understanding this matters more than the script itself, because when something
goes wrong you'll be running these by hand.

```bash
cd /opt/dragonhoard
git pull origin main
```
Fetches the new commits from GitHub and fast-forwards the local `main` to match.
This *only* works cleanly if the directory has no local edits - which is exactly
why the golden rule exists.

```bash
sudo -u dragonbot venv/bin/pip install -r requirements.txt
```
Installs anything new in `requirements.txt`. Almost always a no-op, but it's
free, and skipping it is how you get a `ModuleNotFoundError` at 1am. The
`sudo -u dragonbot` runs it as the bot's own user so the installed files are
owned correctly.

```bash
sudo systemctl restart dragonhoard
```
Python loads code once at startup, so the new code isn't live until you restart.

```bash
systemctl status dragonhoard
journalctl -u dragonhoard -n 50
```
Confirm it came back up. `active (running)` is what you want. If it says
`activating (auto-restart)` it is crash-looping - read the journal output.

### If a deploy goes bad

Get back to the previous version fast:

```bash
cd /opt/dragonhoard
git log --oneline -5          # find the commit hash you were on before
git reset --hard <that-hash>
sudo systemctl restart dragonhoard
```

`reset --hard` throws away local changes to get exactly to that commit - safe
here precisely because this directory is supposed to have no local changes.

Then fix the problem properly in beta, and when GitHub's `main` is good again,
run `git pull origin main` to rejoin it.

**This does not roll back the database.** If a bad release corrupted player
data, the code rollback won't undo it. Which is why:

## Part 4: Back up before you deploy

The live database is the one thing here that can't be recreated from GitHub.
`update.sh` already takes a backup on every run, but to take one by hand:

```bash
cd /opt/dragonhoard
sudo -u dragonbot venv/bin/python -c "
import sqlite3
src = sqlite3.connect('data/dragonhoard.db')
dst = sqlite3.connect('data/manual-backup.db')
with dst:
    src.backup(dst)
"
```

Note this is a real SQLite backup, not a `cp`. The database runs in WAL mode, so
at any moment some committed data lives in the `-wal` sidecar file rather than
the main `.db`. Plain-copying just the `.db` can capture a torn, half-written
state. `src.backup(dst)` takes a consistent snapshot of the true current
contents, safely, even while the bot is running and writing.

Backups accumulate in `data/`. They're gitignored (`data/*.db`), so they never
get pushed, but do delete old ones occasionally.

### Optional: the sqlite3 command-line tool

Not required for anything above, but genuinely handy for inspecting live data
("what's actually in `server_material_storage` right now?"):

```bash
sudo apt install -y sqlite3
sudo -u dragonbot sqlite3 /opt/dragonhoard/data/dragonhoard.db
```
Then `.tables` to list tables, `.schema users` to see one table's definition,
any `SELECT ...;` to query, and `.quit` to exit. Stick to `SELECT` on the live
database - experiment with writes in beta.

## Part 5: Copying live data into beta

Sometimes you need to test against real data - "does this migration work on the
actual production database?" You can, because beta's database is a separate file:

```bash
sudo systemctl stop dragonhoard-beta
cd /opt/dragonhoard
sudo -u dragonbot venv/bin/python -c "
import sqlite3
src = sqlite3.connect('data/dragonhoard.db')
dst = sqlite3.connect('/tmp/prod-snapshot.db')
with dst:
    src.backup(dst)
"
sudo install -o isaac -g isaac /tmp/prod-snapshot.db \
  /opt/dragonhoard-beta/data/dragonhoard-beta.db
rm /tmp/prod-snapshot.db
sudo systemctl start dragonhoard-beta
```

(`install` copies the file *and* sets its owner in one step, so the beta service,
running as `isaac`, can write to it. Stopping beta first means it isn't holding
the old database open while you swap the file underneath it.)

The beta bot is now working from a copy of live data, and can do whatever it
likes to it without any risk to the real thing.

To go back to a clean slate, just delete it - the schema is recreated on boot:

```bash
sudo systemctl stop dragonhoard-beta
rm -f /opt/dragonhoard-beta/data/dragonhoard-beta.db*
sudo systemctl start dragonhoard-beta
```

(The `*` matters - it also removes the `-wal` and `-shm` sidecars, which would
otherwise be left behind referring to a database that no longer exists.)

## Command reference

Everything takes a service name, so the only difference between operating on
live and beta is which name you type.

| Task | Production | Beta |
|---|---|---|
| Start | `sudo systemctl start dragonhoard` | `sudo systemctl start dragonhoard-beta` |
| Stop | `sudo systemctl stop dragonhoard` | `sudo systemctl stop dragonhoard-beta` |
| Restart | `sudo systemctl restart dragonhoard` | `sudo systemctl restart dragonhoard-beta` |
| Is it up? | `systemctl status dragonhoard` | `systemctl status dragonhoard-beta` |
| Live logs | `journalctl -u dragonhoard -f` | `journalctl -u dragonhoard-beta -f` |
| Recent logs | `journalctl -u dragonhoard -n 50` | `journalctl -u dragonhoard-beta -n 50` |
| Errors only | `journalctl -u dragonhoard -p err` | `journalctl -u dragonhoard-beta -p err` |
