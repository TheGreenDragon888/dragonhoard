#!/bin/bash
#
# update.sh - bring the live bot up to date with GitHub's main branch.
#
# Usage:  /opt/dragonhoard/update.sh
#
# See docs/testing.md for the full explanation of each step, and for how to
# roll back if a deploy goes wrong.

# Stop immediately if any command fails, instead of blundering on to the next
# one. Without this, a failed `git pull` would still restart the service.
set -e

cd /opt/dragonhoard

echo "==> Checking for local changes..."
# This directory is a deployment target and should never have local edits.
# If it does, a pull could conflict or silently clobber work, so stop and
# let a human decide.
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: /opt/dragonhoard has uncommitted local changes."
    echo "       Production should be a clean copy of main. Review them with:"
    echo "           cd /opt/dragonhoard && git status && git diff"
    echo "       Then either move the work to /opt/dragonhoard-beta and commit"
    echo "       it there, or discard it with: git reset --hard origin/main"
    exit 1
fi

echo "==> Recording current version (in case you need to roll back)..."
git log --oneline -1

echo "==> Backing up the database..."
# A real SQLite backup, not a cp - the database is in WAL mode, so a plain file
# copy can catch it mid-write and produce a torn snapshot. See docs/testing.md
# Part 4. This uses Python's built-in sqlite3 module rather than the sqlite3
# command-line tool, so there's no extra package to install.
BACKUP="data/backup-$(date +%F-%H%M%S).db"
sudo -u dragonbot venv/bin/python -c "
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
" data/dragonhoard.db "$BACKUP"
echo "    saved to $BACKUP"

echo "==> Pulling from GitHub..."
git pull origin main

echo "==> Installing dependencies..."
sudo -u dragonbot venv/bin/pip install --quiet -r requirements.txt

echo "==> Restarting the bot..."
sudo systemctl restart dragonhoard

# systemd returns from `restart` as soon as the process is spawned, which is
# before Python has imported discord.py, let alone logged in. Give it a moment
# so the status below reflects whether it actually stayed up.
sleep 5

echo "==> Status:"
systemctl status dragonhoard --no-pager -n 20
