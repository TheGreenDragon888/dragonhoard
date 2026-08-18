"""
data/notifications.py

The global announcements a release ships with, in the same shape
data/changelog.py holds the release notes and data/manual.py holds the manual:
this file is all the text, and utils/notifications.py only decides how it gets
on screen.

A global notice is the bot talking about itself to everybody - an announcement,
or a disclaimer about something that went wrong. Each player sees the newest one
once, the next time they run a command, and never again.

Adding one is a single GlobalNotice(...) entry at the TOP of the tuple below,
plus a `key` nobody has used before - newest first, the same way VERSIONS in
data/changelog.py reads. Nothing else needs touching; bot.py seeds these at
startup.

Newest-first is why seed_global_notices walks this tuple BACKWARDS. What players
are shown is the highest notification_id, so the newest entry has to be inserted
last. It only matters when several are seeded in one pass - a database that
already has the older ones gives a newly added notice a fresh high id whatever
the order - but "only matters on a fresh database" is precisely the case nobody
tests by hand.

Notices are kept here forever once shipped, not deleted after a release. The
table is the record of what was announced, and seeding is idempotent on `key`,
so leaving them costs one skipped INSERT each at boot.

Editing the text of a notice that has already gone out will NOT update it - see
seed_global_notices. People have already read it, so a correction is a new
notice with a new key rather than a rewrite of the old one.

Server notifications are not here. Those belong to one guild and are raised at
runtime by whatever feature needs them (utils/notifications.py:
post_server_notification), not shipped with a release.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class GlobalNotice:
    key: str      # stable, unique, never reused - what makes seeding idempotent
    title: str    # the embed title
    body: str     # the embed description


GLOBAL_NOTICES: tuple[GlobalNotice, ...] = (
    GlobalNotice(
        key="1.2.1-update",
        title="Dragonhoard 1.2.1 Update",
        body=(
            "I'm now back from Washington, so...\n\n"
            "**Gem drills are now much faster!** Ruby, Obsidian and Diamond Drills all mine "
            "much more per hour now, and their respective containers grew to match so filling "
            "one still takes around the same amount of time.\n\n"
            "`/recipe factory` shows the new values.\n\n"
            "Thanks y'all for continuin' playin'.\n\n"
            "— Isaac"
        ),
    ),
    GlobalNotice(
        key="1.2-update",
        title="Dragonhoard 1.2 Update",
        body=(
            "**Mining Focuses are now available!** For those fortunately enough "
            "to obtain a ruby can now spend it to change what raw materials "
            'they want to "focus" on mining. Once they spend one, future changes '
            "are free but limited to one per day.\n\n"
            "There's been a few other balance changes as well—including most "
            "notably the departure of gemstones from the server market. "
            "Server's minting currency for gemstones went against the game's "
            "direction for a universally engaging economy, and incentivised the "
            "first player to obtain a gemstone to sell and hoard the server's "
            "wealth individually. This obviously is not fun, and has thusly "
            "been removed. All rubies sold to the market should've been "
            "refunded, and currency withdrawn from players that sold them.\n\n"
            "Thanks for enjoying my game. Feel free to continue sharing your "
            "suggestions on how I can make it better. I'll be gone until Monday "
            "because I'll be with my girlfriend in Washington. So, I apologize "
            "for any bugs or unexpected behavior that I won't be able to patch "
            "until I get back.\n\n"
            "— Isaac"
        ),
    ),
)
