# The testing environment, and how to ship to production

Dragonhoard runs in two completely separate installations on the server, and
is written on a third machine: your own computer. Nothing is shared between
the two installations - not the code checkout, not the Python packages, not the
database, not the Discord application.

| | **Production** | **Beta / testing** | **Your computer** |
|---|---|---|---|
| Directory | `/opt/dragonhoard` | `/opt/dragonhoard-beta` | wherever you cloned it |
| Git branch | `main` | `beta` | `beta`, or a branch of it |
| Gets new code by | `update.sh`, run by hand | `update-beta.sh`, run by a timer every two minutes | you writing it |
| Discord app | Dragonhoard | Dragonhoard Beta | none - see 1f |
| Database | `data/dragonhoard.db` | `data/dragonhoard-beta.db` | none - each test builds its own |
| systemd service | `dragonhoard` | `dragonhoard-beta` | - |
| Runs as user | `dragonbot` | `isaac` | you |
| Slash commands | synced globally (slow) | synced to one test guild (instant) | - |
| Custom emoji | each icon's `live_id` | each icon's `beta_id` - see 1c below | - |
| `.env` → `BOT_ENVIRONMENT` | unset (defaults to `live`) | `beta` | - |

The golden rule: **you never edit code on the server.** Both directories there
are deployment targets. Each one's only job is to be an exact copy of one
branch on GitHub: `/opt/dragonhoard` of `main`, `/opt/dragonhoard-beta` of
`beta`. All editing happens on your own computer, and code reaches the server
only by going through GitHub:

```
your computer --git push--> GitHub: beta --(within a few minutes, by itself)--> /opt/dragonhoard-beta
                                   |
                         release, when beta has passed QA (Part 2)
                                   v
                            GitHub: main --(update.sh, by hand)--> /opt/dragonhoard
```

This is the second version of this workflow. The first had you edit code in
`/opt/dragonhoard-beta` itself, over SSH, and on a low-end laptop that meant
VS Code's server, Claude Code and the test suite all competed with the live
services for the same CPU and memory. Now the server only runs the bots; the
writing happens on your computer and the tests run there and on GitHub.

The rule also keeps file permissions right. Editing as `isaac` inside
`/opt/dragonhoard` resets group ownership on files the `dragonbot` user needs
to read, which crash-loops the live service. If you only ever *pull* into that
directory, git writes the files and ownership stays correct.

## Part 1: One-time setup

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

(`.env` is the one file on the server you do edit by hand. It holds secrets,
so it is never committed to git and never arrives by `git pull`.)

**Make a separate test server.** If you invite Dragonhoard Beta into the same
server as the live bot, you'll see two nearly identical `/mine` commands in the
autocomplete list and you will run the wrong one. A private server with just you
in it is the right setup.

### 1b. Install the beta service

```bash
sudo cp /opt/dragonhoard-beta/dragonhoard-beta.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dragonhoard-beta
sudo systemctl status dragonhoard-beta
```

`enable` means "start on boot", and `--now` also starts it immediately. This
used to be a plain `start` with no `enable`, because the beta checkout was
where code got edited, and a half-finished experiment shouldn't come back up by
itself after a reboot. That reason went away when editing moved to your own
computer: beta now only ever runs what you pushed to the `beta` branch to be
tested, so it runs around the clock like the live bot.

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
   somewhere, or read it back from the Developer Portal) and, on your own
   computer, fill it into that item's `beta_id` in `data/materials.py`.
4. Commit and push to `beta` (Part 2). Beta restarts on its own within a few
   minutes; check the icon renders where you'd expect.

`config.IS_BETA` (from `BOT_ENVIRONMENT` in `.env`) is what picks `beta_id`
over `live_id` - that's also how the bot tells which of the two applications
it's running as everywhere else this matters.

### 1d. Create the `beta` branch and point the beta checkout at it

Create the branch on GitHub: open the repository page, click the branch
dropdown (it says **main**), type `beta`, and click **Create branch beta from
main**. It starts as an exact copy of `main`.

Then, on the server, make sure the beta checkout isn't holding any work before
you move it. This is the last time there should be anything to find here:

```bash
cd /opt/dragonhoard-beta
git status                            # should end: "nothing to commit, working tree clean"
git fetch origin                      # download GitHub's latest, without changing any files
git log --oneline origin/main..HEAD   # commits here that GitHub's main doesn't have - should print nothing
```

`origin/main..HEAD` means "commits reachable from what's checked out here
(`HEAD`) that aren't on GitHub's `main`". If either command shows anything, save
it to GitHub as a branch of its own before going on. That costs nothing, and it
may turn out to be work that's already in `main`: a branch that was squashed
into one "Version" commit still lists its original commits here, because
squashing copied their changes rather than the commits themselves. Look through
it on your computer later, and either merge it into `beta` (Part 2, "Bigger
features") or delete it:

```bash
git switch -c server-wip                 # a new branch holding exactly what's here
git add -A                               # stage every change, including new files
git commit -m "Work in progress from the server"
git push -u origin server-wip            # -u: remember GitHub as this branch's home
```

Now move the checkout to `beta`:

```bash
git switch beta
```

`switch` moves a checkout to another branch. There is no `beta` here yet, only
GitHub's, so git makes a local `beta` that tracks it. `git status` should now
say `Your branch is up to date with 'origin/beta'`.

Last, check how this checkout talks to GitHub:

```bash
git remote -v
```

If the addresses start with `git@github.com:`, switch them to HTTPS:

```bash
git remote set-url origin https://github.com/TheGreenDragon888/dragonhoard.git
```

The repository is public, so fetching over HTTPS needs no key and no password -
which matters because the timer in 1e fetches with nobody around to unlock an
SSH key. It also means this checkout can no longer push, and it shouldn't need
to: code only arrives here.

### 1e. Turn on automatic beta updates

Three files in `deploy/` do this. They are copied out of the checkout into the
system, so if one of them ever changes in git, copy it again.

First, allow `isaac` to restart the two beta services without a password, since
the timer runs with nobody there to type one:

```bash
cd /opt/dragonhoard-beta
command -v systemctl                                        # should print /usr/bin/systemctl
sudo visudo -cf deploy/sudoers-dragonhoard-beta             # check the file before installing it
sudo install -m 0440 -o root -g root deploy/sudoers-dragonhoard-beta /etc/sudoers.d/dragonhoard-beta
sudo -k; sudo -n systemctl restart dragonhoard-beta && echo "rule works"
```

- `command -v systemctl` shows where `systemctl` lives. The rule names that
  exact path; if yours prints something else, change the file to match.
- `visudo -cf` checks a sudoers file for mistakes (`-c` check only, `-f` this
  file). A broken file in `/etc/sudoers.d` can stop `sudo` working at all, so
  always check before installing.
- `install` copies the file and sets its owner and permissions in one step;
  `0440` (read-only, owner and group only) is what sudo requires of these files.
- `sudo -k` forgets your recent password, and `-n` makes sudo fail rather than
  ask for one - so if this prints `rule works`, the rule did it, not your
  password. If it says `a password is required`, the rule isn't matching.

The rule allows exactly `systemctl restart dragonhoard-beta` and
`systemctl restart dragonhoard-beta-web`, and nothing else.

Then install the timer:

```bash
sudo cp deploy/dragonhoard-beta-update.service deploy/dragonhoard-beta-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dragonhoard-beta-update.timer
systemctl list-timers dragonhoard-beta-update.timer
```

You enable the **timer**, not the service: the timer is what starts the service
every two minutes. `list-timers` shows when it last ran (`LAST`) and will next
run (`NEXT`). Part 6 explains what each run does.

### 1f. Set up your own computer

Install:

- **Git** - on Windows, [Git for Windows](https://git-scm.com/download/win). It
  includes Git Credential Manager, which signs you in to GitHub through your
  browser the first time you push.
- **Python 3.12 or newer** - on Windows, from [python.org](https://www.python.org/downloads/);
  tick **Add python.exe to PATH** in the installer. It has to be 3.12 or later:
  `cogs/mining.py` puts a backslash inside an f-string expression, which 3.11
  and older refuse to parse. Ideally use the same version as the server - run
  `python3 --version` there to see it.
- **VS Code**, and Claude Code if you use it.

Then, in a terminal (PowerShell on Windows):

```bash
git config --global user.name "Your Name"          # who your commits say wrote them
git config --global user.email "you@example.com"   # use the email on your GitHub account
git clone https://github.com/TheGreenDragon888/dragonhoard.git
cd dragonhoard
git switch beta
```

Make a virtual environment and install everything the tests need.

On Windows:

```powershell
py -3.12 -m venv venv
venv\Scripts\python -m pip install -r requirements-dev.txt -r requirements-web.txt
```

On macOS or Linux:

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements-dev.txt -r requirements-web.txt
```

`requirements-web.txt` is there because `tests/test_ops_dashboard.py` tests the
dashboard in `web/`.

Last, create a file called `.env` in the `dragonhoard` folder (in VS Code:
**File -> New File**, save it as `.env`) containing one line:

```
DISCORD_BOT_TOKEN=placeholder-for-tests
```

The tests import `config.py`, which refuses to load without a token. They never
connect to Discord, so any value works. `.env` is in `.gitignore`, so it stays
on your computer.

Check it all works:

```bash
venv/bin/python -m pytest tests/ -q           # Windows: venv\Scripts\python -m pytest tests/ -q
```

`tests/conftest.py` puts the test databases in `/dev/shm` when there is one.
Windows and macOS don't have it, so there they go in your normal temp folder -
if a run seems slow, that's why.

**Don't run the bot itself with Dragonhoard Beta's token on your computer.**
Beta is already logged in with it on the server, so Discord would deliver
commands to both copies and they would race to answer, each against its own
database. If you want to run the bot locally, create a third application in the
Developer Portal (say, "Dragonhoard Dev") with its own token and its own test
server, and put those in your computer's `.env`. Its custom icons won't render,
for the reason in 1c. Most of the time you won't need it: the tests cover the
game logic, and beta is where you try things in Discord.

## Part 2: The day-to-day loop

### Write and test, on your computer

```bash
git switch beta
git pull                                   # pick up anything pushed from elsewhere
# ... make your changes, or have Claude Code make them ...
venv/bin/python -m pytest tests/ -q        # Windows: venv\Scripts\python -m pytest tests/ -q
git add -A
git commit -m "Rework mining pool top-up rates"
```

A commit stays on your computer until you push, so commit as often as you like.

### Send it to beta

```bash
git push
```

That's the whole deploy. Within a few minutes the timer notices the new commit,
pulls it into `/opt/dragonhoard-beta` and restarts the beta bot. Meanwhile
GitHub runs the test suite on it (`.github/workflows/tests.yml`): a green tick
or red cross appears next to the commit on GitHub, and the **Actions** tab has
the details.

To watch it land, over SSH on the server:

```bash
journalctl -u dragonhoard-beta-update -n 30   # what the updater did
journalctl -u dragonhoard-beta -f             # the beta bot booting; Ctrl+C stops watching
```

Or run `/opt/dragonhoard-beta/update-beta.sh` yourself to skip the wait.

Then go poke at it in your test server. Because `DEV_GUILD_ID` is set, any new
or renamed slash command shows up the moment the bot finishes booting.

### Bigger features: a branch of their own

Everything on `beta` gets released together. If something bigger might not be
finished by the time you want to release, keep it off `beta` until it's ready:

```bash
git switch -c mining-rework        # a new branch, starting from where beta is
# ... commit as usual ...
git push -u origin mining-rework   # backs it up to GitHub without deploying it
git switch beta
git merge mining-rework            # when it's ready to be tested live
git push
git branch -d mining-rework        # tidy up the finished branch
```

Claude Code sessions on the web already work this way: they push to a
`claude/...` branch. If one opens a pull request, set its base to `beta`, not
`main`, before merging it - otherwise it skips beta entirely.

### Before releasing: QA

Release only when all three hold:

1. The newest commit on `beta` has GitHub's green tick.
2. You've tried the changes yourself in the test server.
3. The live database survives the upgrade. Copy live data into beta (Part 5)
   and watch it boot. Beta's database took each schema change as it arrived;
   production's will take every change since the last release at once, on
   real data.

### Release: move `main` up to `beta`

On your computer:

```bash
git switch beta && git pull        # make sure you have the newest beta...
git switch main && git pull        # ...and the newest main
git merge --ff-only beta           # make main exactly equal to beta
git push
```

`--ff-only` (fast-forward only) moves `main` forward to the very same commit as
`beta`, adding nothing of its own, and refuses if `main` has a commit `beta`
doesn't - a hotfix you haven't carried into beta yet (below). A refusal is the
safety net working: carry `main` into `beta`, check it on beta, try again.

Mark the version with a tag, so you can find where it starts later:

```bash
git tag v1.5
git push origin v1.5
```

Until now each version landed on `main` as one commit. Now `main` receives
beta's commits as they are, and the tag is what marks the version. **Never
squash or rebase `beta` into `main`** (GitHub's **Squash and merge** and
**Rebase and merge** buttons). Both write new copies of beta's commits onto
`main`, so the two branches stop sharing history, and later releases run into
conflicts. To take those buttons away entirely: GitHub repository **Settings ->
General -> Pull Requests**, untick **Allow squash merging** and **Allow rebase
merging**.

### Update production

Now, and only now, touch the live bot, on the server:

```bash
/opt/dragonhoard/update.sh
```

That script pulls `main`, installs any new dependencies, restarts the service,
and shows you the status. It's the whole deploy. See Part 3 for what it does
step by step and how to undo it.

### Hotfixes: fixing live without releasing beta

`beta` may be carrying work that isn't ready, so an urgent fix to the live bot
starts from `main` instead:

```bash
git switch main && git pull
git switch -c hotfix-market-cancel
# ... fix it, run the tests ...
git add -A
git commit -m "Fix /market cancel rejecting non-integer ids"
git push -u origin hotfix-market-cancel     # GitHub tests it; wait for the tick
git switch main
git merge --ff-only hotfix-market-cancel
git push
```

Run `/opt/dragonhoard/update.sh` on the server. Then carry the fix into beta, or
the next release will refuse (see `--ff-only` above) - and worse, beta would be
testing code without it:

```bash
git switch beta && git pull
git merge main                     # the one merge commit this workflow expects
git push
git branch -d hotfix-market-cancel
```

If the fix touched lines that beta also changed, `git merge` stops and asks you
to resolve the conflict first. A hotfix reaches live without a stay on beta -
only the tests and your own check stand between it and players - so keep them
small.

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

Then fix the problem properly on your computer (a hotfix - Part 2), and when
GitHub's `main` is good again, run `git pull origin main` to rejoin it.

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
the old database open while you swap the file underneath it. A push landing
mid-swap won't start it early: `update-beta.sh` leaves a stopped beta stopped -
see Part 6.)

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

## Part 6: What `update-beta.sh` actually does

The timer from 1e runs `/opt/dragonhoard-beta/update-beta.sh` every two minutes,
as `isaac`. Almost every run finds nothing new and ends silently. When there is
something new, step by step:

```bash
cd /opt/dragonhoard-beta
git rev-parse --abbrev-ref HEAD          # must print "beta"
git diff --quiet && git diff --cached --quiet
```
Refuses to go on if the checkout is on another branch, or has edits that were
never committed. Either means someone is working here by hand, which the
golden rule says shouldn't happen - so it stops and lets you decide rather than
guessing.

```bash
git fetch origin beta
```
Downloads any new commits on GitHub's `beta` without changing a single file
here yet. If `origin/beta` (git's record of where GitHub's `beta` is) now
matches `HEAD` (what's checked out), there's nothing to do and the run ends.

```bash
git merge-base --is-ancestor HEAD origin/beta
```
Checks that GitHub's `beta` still *contains* what's checked out here - that is,
that `beta` only moved forward. It won't if somebody force-pushed `beta`
(rewrote it). Then the script stops rather than throw away what's here; the
error tells you the one command that takes GitHub's version if that's what you
want.

```bash
git merge --ff-only origin/beta
```
Moves the files forward to GitHub's `beta`. Fast-forward only, so it never
invents a commit of its own.

```bash
venv/bin/pip install -r requirements-dev.txt -r requirements-web.txt
```
Only when a push changed a `requirements*.txt` file, since even a no-op install
costs a few seconds of CPU.

```bash
sudo systemctl restart dragonhoard-beta
sudo systemctl restart dragonhoard-beta-web
```
Each only if that service isn't stopped. `systemctl is-active` reports a
service's state: `active` (running), `activating` (starting, or waiting to
restart after a crash), `failed` (crashed and given up) or `inactive` (stopped,
or never started). Anything but `inactive` is restarted, so a pushed fix brings
a crashed beta back. `inactive` is left alone, because that's a service you
stopped on purpose (or, for `dragonhoard-beta-web`, never installed); it picks
up the new code whenever you start it.

The script does **not** run the tests - GitHub does that on every push, which
is what keeps them off the server.

Where to look:

```bash
journalctl -u dragonhoard-beta-update -n 30          # what recent runs did
systemctl status dragonhoard-beta-update             # did the last run fail?
systemctl list-timers dragonhoard-beta-update.timer  # when it runs next
```

To pause automatic updates (say, to hold beta on one version while you test
it), stop the timer; start it again to resume. A stopped timer comes back at the
next reboot; `sudo systemctl disable --now dragonhoard-beta-update.timer` stops
it and keeps it off until you `enable --now` it again.

```bash
sudo systemctl stop dragonhoard-beta-update.timer
sudo systemctl start dragonhoard-beta-update.timer
```

## Command reference

Everything takes a service name, so the only difference between operating on
live and beta is which name you type.

| Task | Production | Beta |
|---|---|---|
| Deploy new code | `/opt/dragonhoard/update.sh` | `git push` to `beta`, from your computer |
| Deploy right now | - | `/opt/dragonhoard-beta/update-beta.sh` |
| Start | `sudo systemctl start dragonhoard` | `sudo systemctl start dragonhoard-beta` |
| Stop | `sudo systemctl stop dragonhoard` | `sudo systemctl stop dragonhoard-beta` |
| Restart | `sudo systemctl restart dragonhoard` | `sudo systemctl restart dragonhoard-beta` |
| Is it up? | `systemctl status dragonhoard` | `systemctl status dragonhoard-beta` |
| Live logs | `journalctl -u dragonhoard -f` | `journalctl -u dragonhoard-beta -f` |
| Recent logs | `journalctl -u dragonhoard -n 50` | `journalctl -u dragonhoard-beta -n 50` |
| Errors only | `journalctl -u dragonhoard -p err` | `journalctl -u dragonhoard-beta -p err` |
| Updater log | - | `journalctl -u dragonhoard-beta-update -n 30` |
| Pause auto-updates | - | `sudo systemctl stop dragonhoard-beta-update.timer` |
