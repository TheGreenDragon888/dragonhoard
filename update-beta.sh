#!/bin/bash
#
# update-beta.sh - bring the beta bot up to date with GitHub's beta branch.
#
# Usage:  /opt/dragonhoard-beta/update-beta.sh
#
# Run it over SSH after pushing to beta. Or install the optional timer
# (docs/testing.md Part 1e), which runs it every two minutes so that pushing
# to beta is the whole deploy. See docs/testing.md Part 6 for each step
# explained.

# Stop at the first failing command, the same as update.sh.
set -e

# Bash reads a script a little at a time as it runs, and the merge below can
# replace this very file mid-run. Everything lives in main(), called on the
# last line, so bash has read all of it before any of it runs.
main() {
    cd /opt/dragonhoard-beta

    # This checkout is a deployment target that follows beta and nothing else.
    # On any other branch someone is doing something here by hand, so leave it
    # alone rather than yank the branch out from under them.
    branch=$(git rev-parse --abbrev-ref HEAD)
    if [ "$branch" != "beta" ]; then
        echo "ERROR: /opt/dragonhoard-beta is on branch '$branch', not 'beta'."
        echo "       Switch it back with: cd /opt/dragonhoard-beta && git switch beta"
        exit 1
    fi

    # The same guard as update.sh: code is edited on your own computer, never
    # here, so a local edit is either a mistake or unpushed work. Stop either way.
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "ERROR: /opt/dragonhoard-beta has uncommitted local changes."
        echo "       Review them with: cd /opt/dragonhoard-beta && git status && git diff"
        echo "       Then either commit them on your own computer instead, or"
        echo "       discard them here with: git reset --hard origin/beta"
        exit 1
    fi

    git fetch --quiet origin beta
    old=$(git rev-parse HEAD)
    new=$(git rev-parse origin/beta)

    if [ "$old" = "$new" ]; then
        # Only say so when a person ran this in a terminal; the optional timer
        # runs it every two minutes and the journal doesn't need a line for
        # each one.
        if [ -t 1 ]; then
            echo "Already up to date: $(git --no-pager log --oneline -1)"
        fi
        return 0
    fi

    # Only ever move forward. If GitHub's beta doesn't contain what is checked
    # out here, beta was force-pushed (or someone committed here), and a person
    # should decide which version wins.
    if ! git merge-base --is-ancestor HEAD origin/beta; then
        echo "ERROR: GitHub's beta branch no longer contains this checkout's commit."
        echo "       Either beta was force-pushed, or someone committed in /opt/dragonhoard-beta."
        echo "       To throw this checkout's version away and take GitHub's:"
        echo "           cd /opt/dragonhoard-beta && git reset --hard origin/beta"
        exit 1
    fi

    echo "==> New commits on beta:"
    git --no-pager log --oneline "$old..$new"
    git merge --ff-only --quiet origin/beta

    # Reinstalling takes a few seconds of CPU even when nothing changes, so it
    # only runs when a push touched a requirements file.
    if ! git diff --quiet "$old" "$new" -- 'requirements*.txt'; then
        echo "==> Requirements changed, installing..."
        venv/bin/pip install --quiet -r requirements-dev.txt -r requirements-web.txt
    fi

    echo "==> Restarting..."
    restart_unless_stopped dragonhoard-beta
    restart_unless_stopped dragonhoard-beta-web

    # The same wait as update.sh: systemd returns before the bot has logged in.
    sleep 5
    echo "==> Status:"
    systemctl status dragonhoard-beta --no-pager -n 20 || true
}

# A service you stopped on purpose ("inactive") stays stopped: this runs every
# couple of minutes, and would otherwise start beta back up in the middle of
# something like docs/testing.md Part 5's database swap. A running,
# crash-looping or crashed service is restarted, so a pushed fix revives it.
# A service that isn't installed at all also reads "inactive" and is skipped.
restart_unless_stopped() {
    local unit=$1
    local state
    state=$(systemctl is-active "$unit" || true)
    if [ "$state" = "inactive" ]; then
        echo "    $unit is stopped - leaving it stopped. It runs the new code when you start it."
    else
        # Run by hand, this asks for your password. Under the optional timer,
        # /etc/sudoers.d/dragonhoard-beta (docs/testing.md Part 1e) allows
        # these two commands, and nothing else, without one.
        sudo systemctl restart "$unit"
        echo "    restarted $unit"
    fi
}

main "$@"; exit
