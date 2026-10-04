"""
utils/notifications.py

One-off notices a player sees the next time they use the bot, and only once.

Two BROADCAST feeds (the `notifications` table in schema.sql):

  global - from the bot itself. Announcements and disclaimers that ship with a
           release, written as data in data/notifications.py and seeded at
           startup. Read once per USER, so somebody in five servers sees one
           once rather than five times.
  server - belongs to one guild. Posted at runtime by a feature via
           post_server_notification. Read once per user PER GUILD.

Only the NEWEST notice of each broadcast feed is ever shown. A player who has
been away for three announcements gets the third, not all three - the read
marker stores an id rather than a set, so everything below it is skipped by
construction. Superseded rows are kept as a record of what was announced and
when; the rule is about brevity on screen, not about retention.

And PERSONAL notices (the `user_notifications` table), which are a different
shape and get their own table for it: the row belongs to one player, so its
read state lives on the row rather than in a per-feed watermark, and nothing is
skipped for being superseded - two things that happened to you are two things
you should be told. Raised at runtime by a feature via post_user_notification,
with the wording in data/notifications.py alongside the global announcements.
The first gemstone of a kind a player obtains is the only thing that raises one
so far (utils/db_helpers.py: announce_first_gem).

One exception to "two things, two notices": a feature can keep a personal notice
open and rewrite it while it is still unread (set_user_notification), when the
things it reports are many of one kind and a running total says it better than
a stack of identical embeds. Repaid bonds do this - every bond the server
finishes repaying before the player next looks is one notice whose count and
total grow (utils/government.py: announce_repaid_bonds). A personal notice can
also belong to one server (user_notifications.guild_id) and then waits until the
player is next there; those bond totals are in that server's currency.

Delivery hangs off utils/responses.py: respond(), which is the single place a
command's SUCCESSFUL response is sent. That is a deliberate choice of hook.
Error and validation replies go through interaction.response.send_message
directly, and bolting an announcement onto "you only have 3 of that item" reads
as though the two are related. It does mean a player whose every command fails
never sees a notice, which is the right trade.

A notice can also carry an ACTION - `notifications.action_key`, an opaque
"<kind>:<id>" string naming something the reader can do about it. Delivery
turns that into buttons on the very reply the notice rides in on; the registry
that maps a kind to its buttons lives in utils/responses.py. Prediction bets
(1.4) are the only thing raising one so far: a bet's two "back it"/"oppose it"
buttons reach a server's players this way rather than through a channel post.

Marking happens AFTER the send succeeds, so the delivery is at-least-once: if
Discord rejects the message the notice is still pending and will ride along with
the next command. Marking first would make it at-most-once, and silently losing
an announcement is worse than showing one twice.

Not every pending notice need arrive on the same reply. A message holds only so
many embeds, and nothing caps how many personal notices a player can be owed, so
respond() attaches the ones that fit and marks only those; the rest come with
the commands after (utils/responses.py: _notices_that_fit).
"""
import logging

import discord

from database.db import Database, _Executor
from utils.embeds import (
    make_embed,
    GLOBAL_NOTICE_COLOR,
    PERSONAL_NOTICE_COLOR,
    SERVER_NOTICE_COLOR,
)

log = logging.getLogger("dragonhoard")

# The guild_id standing for the global feed in notification_reads. See the
# table's comment in schema.sql for why this is a sentinel and not NULL.
GLOBAL_FEED_ID = 0

# Only the newest of each scope, and only if this user hasn't already seen it.
# The MAX() subqueries return NULL for an empty feed, which is harmless here:
# `id IN (NULL, 7)` is true for 7 and merely unknown (never true) for anything
# else, so a bot with no global notices at all simply matches nothing.
_UNSEEN_SQL = """
SELECT n.notification_id, n.scope, n.guild_id, n.title, n.body, n.action_key
FROM notifications n
LEFT JOIN notification_reads r
       ON r.user_id = ?
      AND r.guild_id = CASE n.scope WHEN 'global' THEN 0 ELSE n.guild_id END
WHERE n.notification_id IN (
        SELECT MAX(notification_id) FROM notifications WHERE scope = 'global'
        UNION ALL
        SELECT MAX(notification_id) FROM notifications WHERE scope = 'server' AND guild_id = ?
      )
  AND (r.last_seen_id IS NULL OR r.last_seen_id < n.notification_id)
ORDER BY CASE n.scope WHEN 'global' THEN 0 ELSE 1 END
"""


async def fetch_unseen(db: Database, user_id: int, guild_id: int | None) -> list:
    """The notices this user hasn't seen yet: at most one per feed, global
    first. Returns rows rather than embeds so the ordering and the read markers
    can be tested without discord.py.

    A guild_id of None (a DM) still matches the global feed - `guild_id = NULL`
    is never true for the server subquery, so it falls out on its own without a
    branch here."""
    return await db.fetchall(_UNSEEN_SQL, (user_id, guild_id))


async def mark_seen(db: _Executor, user_id: int, rows) -> None:
    """Records that this user has now seen `rows`, one marker per feed.

    The marker only ever moves forward. Two commands racing can both fetch the
    same notice and both mark it, and a slow one can commit after a newer notice
    has already been marked by a faster one - the guard on the upsert is what
    stops the second case from un-reading an announcement the player has already
    been shown."""
    for row in rows:
        feed_id = GLOBAL_FEED_ID if row["scope"] == "global" else row["guild_id"]
        await db.execute(
            "INSERT INTO notification_reads (user_id, guild_id, last_seen_id) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, guild_id) DO UPDATE SET last_seen_id = excluded.last_seen_id "
            "WHERE excluded.last_seen_id > notification_reads.last_seen_id",
            (user_id, feed_id, row["notification_id"]),
        )


# Color and author line per scope. Each kind gets its own (see
# docs/stylization.md) because they carry different authority - the bot talking
# about itself, one guild's own business, or something that happened to you -
# and a player should be able to tell which without reading. All three can
# arrive in the same message, so they have to differ from each other as well as
# from whatever the reply itself is colored.
_NOTICE_STYLE = {
    "global": (GLOBAL_NOTICE_COLOR, "Dragonhoard announcement"),
    "server": (SERVER_NOTICE_COLOR, "Server notice"),
    "user": (PERSONAL_NOTICE_COLOR, "For you"),
}


def notice_embed(row) -> discord.Embed:
    """One notice as its own embed, styled by its scope."""
    color, author = _NOTICE_STYLE[row["scope"]]
    embed = make_embed(row["title"], color, description=row["body"])
    embed.set_author(name=author)
    return embed


async def post_server_notification(
    db: _Executor, guild_id: int, title: str, body: str, action_key: str | None = None
) -> int:
    """Raises a notice for one server, shown once to each of its players. The
    entry point for features that need to tell a whole guild something.

    Returns the new notification_id. Nothing dedupes these - a caller that can
    fire twice for the same event has to guard that itself, because "the same
    title" is not the same thing as "the same event".

    `action_key` attaches something the reader can DO about it - see the action
    registry in utils/responses.py, which turns it into buttons on the same
    reply this notice arrives in. Almost every notice leaves it None and is
    text alone."""
    return await db.execute(
        "INSERT INTO notifications (scope, guild_id, title, body, action_key) "
        "VALUES ('server', ?, ?, ?, ?)",
        (guild_id, title, body, action_key),
    )


async def post_user_notification(
    db: _Executor, user_id: int, notice_key: str, title: str, body: str
) -> bool:
    """Raises a notice for one player, shown to them once. The entry point for
    features that need to tell a single person something.

    Returns True if this newly raised one, False if that player already has a
    notice under this key. Unlike post_server_notification the dedupe is built
    in, because a personal notice fires off a thing that HAPPENS to a player
    rather than off an admin action - the caller is a hot path that runs on
    every one of those events and only the first should say anything. The
    (user_id, notice_key) primary key is the whole mechanism, so the row is both
    the notice and the record that this player has had it.

    Safe to call inside a caller's transaction, and that is where it belongs:
    the notice should commit or roll back with the thing it is announcing.
    """
    return bool(await db.execute_changes(
        "INSERT OR IGNORE INTO user_notifications (user_id, notice_key, title, body) "
        "VALUES (?, ?, ?, ?)",
        (user_id, notice_key, title, body),
    ))


async def set_user_notification(
    db: _Executor, user_id: int, notice_key: str, title: str, body: str, guild_id: int | None = None
) -> None:
    """Raises a personal notice, or rewrites it if the player hasn't been shown
    it yet. For a notice that reports a running total rather than one event -
    see the module docstring.

    Once the player has seen it the row is left alone: what they read stays the
    record of what they were told, and the caller starts a new notice under a
    new key. `guild_id` limits it to that one server (fetch_unseen_personal).

    Safe inside a caller's transaction, for the reason post_user_notification is.
    """
    await db.execute(
        "INSERT INTO user_notifications (user_id, notice_key, title, body, guild_id) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, notice_key) DO UPDATE SET title = excluded.title, body = excluded.body "
        "WHERE user_notifications.seen_at IS NULL",
        (user_id, notice_key, title, body, guild_id),
    )


async def fetch_unseen_personal(db: Database, user_id: int, guild_id: int | None = None) -> list:
    """Every personal notice this player hasn't been shown yet in this server,
    oldest first: the ones that belong to no server, and the ones that belong
    to `guild_id`. A guild_id of None (a DM) gets only the former, since
    `guild_id = NULL` is never true.

    ALL of them, not just the newest - a personal notice is not a broadcast
    that a later one supersedes. Ordered by when it was raised so a player who
    earned a ruby and then an obsidian reads them in the order they happened;
    rowid breaks a tie, since created_at has one-second resolution and both can
    be raised by the same /collect.
    """
    # NULL AS action_key so a personal notice has the same row shape as a
    # broadcast one and utils/responses.py can read the column off either
    # without asking which it is holding. Nothing raises a personal notice with
    # an action today; the column is on `notifications` alone.
    return await db.fetchall(
        "SELECT notice_key, title, body, 'user' AS scope, NULL AS action_key "
        "FROM user_notifications "
        "WHERE user_id = ? AND seen_at IS NULL AND (guild_id IS NULL OR guild_id = ?) "
        "ORDER BY created_at, rowid",
        (user_id, guild_id),
    )


async def mark_personal_seen(db: _Executor, user_id: int, rows) -> None:
    """Records that this player has now been shown `rows`.

    The row's own seen_at rather than a watermark, so nothing can be skipped by
    a later notice being marked first. Already-set values are left alone - the
    guard means a notice shown twice (see the at-least-once note above) keeps
    the timestamp of when it was first actually read.

    Only if the body is still the one that was shown. set_user_notification can
    rewrite a notice between respond() fetching it and marking it - another
    bond repaid while the reply was in flight - and marking it then would close
    a notice whose newest total the player never saw. Left unread, it comes back
    with that total on their next command."""
    for row in rows:
        await db.execute(
            "UPDATE user_notifications SET seen_at = datetime('now') "
            "WHERE user_id = ? AND notice_key = ? AND body = ? AND seen_at IS NULL",
            (user_id, row["notice_key"], row["body"]),
        )


async def seed_global_notices(db: Database, notices) -> int:
    """Writes the release's global announcements (data/notifications.py) into
    the table, skipping any already there. Called once at startup.

    Idempotent on notice_key, which is what makes it safe to run on every boot:
    without it a bot that restarts nightly would re-announce the same disclaimer
    to everybody each morning. Returns how many were newly inserted.

    Editing the text of a notice that has already shipped deliberately does
    NOT update it. The key identifies an announcement that was made, and people
    have already read it - changing what it said after the fact would mean the
    record no longer matches what anyone saw. A correction is a new notice.

    Walks `notices` backwards because data/notifications.py lists them newest
    first (matching data/changelog.py) while what a player is shown is the
    highest notification_id - so the newest has to be inserted last.
    """
    inserted = 0
    for notice in reversed(notices):
        changed = await db.execute_changes(
            "INSERT OR IGNORE INTO notifications (scope, guild_id, title, body, notice_key) "
            "VALUES ('global', NULL, ?, ?, ?)",
            (notice.title, notice.body, notice.key),
        )
        if changed:
            inserted += 1
            log.info("Seeded global notification %r.", notice.key)
    return inserted
