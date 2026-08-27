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

PERSONAL notices ARE here, at the bottom - GEM_UNLOCK_NOTICES. They are raised
at runtime like a server notice, but unlike one their wording is fixed content
that ships with a release, so it belongs in this file with the rest of the
text rather than inline in the code that fires it.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class GlobalNotice:
    key: str      # stable, unique, never reused - what makes seeding idempotent
    title: str    # the embed title
    body: str     # the embed description


GLOBAL_NOTICES: tuple[GlobalNotice, ...] = (
    GlobalNotice(
        key="1.3-update",
        title="Dragonhoard 1.3 Update",
        body=(
            "**Massive changes!** I'm trying to speedrun this update out, so this "
            "announcement won't be very long; despite this being the biggest single "
            "Dragonhoard update yet.\n\n"
            "**Market prices are now fixed**, **blast furnace** (basically a 100x furnace) has "
            "been added to the server infrastructure `/blast` (`/recipe furnace` for "
            "recipes), **Mining Efficiency** mode has been added (use 1 Obsidian to enable it "
            "similarly to Mining Focus), **the job board can now payout multiple times within "
            "a day** and **extra server mining slots**!! (Which are calculated based on "
            "cumulative server fee investment into all server infrastructure.)\n\n"
            "Holy guac this was a huge undertaking, but it's now out now for y'all to enjoy.\n\n"
            "PEACE!\n\n"
            "— Isaac"
        ),
    ),
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


# ---------------------------------------------------------------------------
# Personal notices
# ---------------------------------------------------------------------------

# EDIT THE WORDING BELOW. This is the whole text of what a player is told the
# first time they get one of these gemstones; nothing else in the codebase
# writes any of it.
#
# Why these two and nothing else: a ruby and an obsidian are the only items in
# the game that unlock a COMMAND. Everything else a player finds is an
# ingredient they will meet again in /recipe, but /focus and /efficiency are
# invisible until you own the gem that opens them - a player can hold a ruby
# for weeks without discovering that it does anything but sit there. That is
# what this notice is for, and it is the bar a third entry should clear.
#
# The key is per-notice, not per-player: utils/db_helpers.py: announce_first_gem
# writes one row per (player, key), and the primary key on user_notifications
# is what makes the second ruby a no-op. Changing a key here re-notifies
# everybody who already has that gem, which is a reasonable way to re-announce
# a reworded hint and a very unreasonable accident - so change it on purpose or
# not at all.
#
# Both are unlocked by spending ONE gem, and the notice says so rather than
# quoting MINING_FOCUS_UNLOCK_COST / MINING_EFFICIENCY_UNLOCK_COST, because a
# player reading this is holding exactly one and the sentence is about what to
# do with it. If those costs ever stop being 1, this text has to change with
# them - tests/test_notifications.py pins that they still match.
@dataclass(frozen=True)
class PersonalNotice:
    key: str      # unique per notice; one row per (user, key) - see above
    title: str    # the embed title
    body: str     # the embed description


GEM_UNLOCK_NOTICES: dict[str, PersonalNotice] = {
    "ruby": PersonalNotice(
        key="first-ruby",
        title="Your First Ruby",
        body=(
            "You've found a **Ruby** - and it's worth more than what it builds.\n\n"
            "Spending one on `/focus` lets you commit your mining to a single ore. "
            "Everything you would have mined of the others arrives as the one you "
            "chose instead, so a drill that was pulling a bit of everything starts "
            "pulling what you actually need.\n\n"
            "You only ever pay the ruby once. Changing your mind afterwards is free, "
            "one change a day.\n\n"
            "Run `/focus` to see what each one does before you spend it."
        ),
    ),
    "obsidian": PersonalNotice(
        key="first-obsidian",
        title="Your First Obsidian",
        body=(
            "You've found an **Obsidian** - the rarest thing most players will ever "
            "hold that isn't a diamond.\n\n"
            "Spending one on `/efficiency` re-proportions what you mine toward a "
            "single recipe: pick Iron, Copper or Steel and your haul arrives in the "
            "amounts the furnace actually wants, instead of in whatever the ground "
            "happened to give you.\n\n"
            "It stacks with your `/focus`, and like a focus you only pay the gem "
            "once - changes afterwards are free, one a day.\n\n"
            "Run `/efficiency` to see what each one does before you spend it."
        ),
    ),
}
