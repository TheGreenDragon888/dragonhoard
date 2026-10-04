"""
Tests for the notification system against a real database.

The rules worth pinning are the ones a player would notice going wrong: a
notice is shown once and not again, only the NEWEST of each BROADCAST feed is
shown, the global feed is per-user while the server feed is per-user-per-guild,
and seeding the same release twice doesn't re-announce anything.

Personal notices are the deliberate exception to the newest-only rule and are
tested for it below: they belong to one player, so none of them is superseded
by a later one and all of them are shown.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import discord

from database.db import Database
from data.materials import (
    MINING_AFFINITY_UNLOCK_COST,
    MINING_EFFICIENCY_UNLOCK_COST,
    MINING_FOCUS_UNLOCK_COST,
)
from data.notifications import GEM_UNLOCK_NOTICES, GLOBAL_NOTICES, GlobalNotice
from utils.db_helpers import (
    adjust_user_quantity,
    announce_first_gem,
    deduct_user_quantity,
    ensure_user_row,
)
from utils.responses import (
    MAX_EMBED_CHARS_PER_MESSAGE,
    MAX_EMBEDS_PER_MESSAGE,
    _merge_embeds,
    respond,
)
from utils.notifications import (
    GLOBAL_FEED_ID,
    fetch_unseen,
    fetch_unseen_personal,
    mark_personal_seen,
    mark_seen,
    notice_embed,
    post_server_notification,
    post_user_notification,
    seed_global_notices,
    set_user_notification,
)

GUILD = 111
OTHER_GUILD = 222
USER = 777
OTHER_USER = 888


class NotificationTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def add_global(self, key, title="Notice", body="Body"):
        return await seed_global_notices(self.db, (GlobalNotice(key, title, body),))

    async def unseen(self, user_id=USER, guild_id=GUILD):
        return await fetch_unseen(self.db, user_id, guild_id)

    async def see(self, user_id=USER, guild_id=GUILD):
        """One full delivery: fetch what's pending and mark it read, the way
        utils/responses.py: respond does."""
        rows = await self.unseen(user_id, guild_id)
        await mark_seen(self.db, user_id, rows)
        return rows


class DeliveryTests(NotificationTestCase):
    async def test_nothing_pending_by_default(self):
        self.assertEqual(await self.unseen(), [])

    async def test_a_notice_is_shown_once_and_then_never_again(self):
        await self.add_global("a")
        self.assertEqual(len(await self.see()), 1)
        self.assertEqual(await self.unseen(), [])
        self.assertEqual(await self.unseen(), [])

    async def test_only_the_newest_of_a_feed_is_shown(self):
        # The brevity rule: somebody away for three announcements gets the
        # third, not a backlog of all three.
        for key in ("a", "b", "c"):
            await self.add_global(key, title=key)
        rows = await self.see()
        self.assertEqual([r["title"] for r in rows], ["c"])

    async def test_a_newer_notice_reaches_someone_already_caught_up(self):
        await self.add_global("a", title="a")
        await self.see()
        await self.add_global("b", title="b")
        self.assertEqual([r["title"] for r in await self.see()], ["b"])

    async def test_both_feeds_arrive_together_global_first(self):
        await self.add_global("a", title="global")
        await post_server_notification(self.db, GUILD, "server", "Body")
        rows = await self.see()
        self.assertEqual([r["title"] for r in rows], ["global", "server"])
        self.assertEqual([r["scope"] for r in rows], ["global", "server"])

    async def test_the_two_feeds_are_marked_independently(self):
        await self.add_global("a", title="global")
        await self.see()
        await post_server_notification(self.db, GUILD, "server", "Body")
        # Seeing the global one must not have consumed a server notice posted
        # afterwards, and vice versa.
        self.assertEqual([r["title"] for r in await self.see()], ["server"])


class FeedScopeTests(NotificationTestCase):
    async def test_a_global_notice_is_read_once_per_user_not_per_server(self):
        await self.add_global("a")
        await self.see(guild_id=GUILD)
        # Same person, different server: they have already read it.
        self.assertEqual(await self.unseen(guild_id=OTHER_GUILD), [])

    async def test_a_server_notice_belongs_to_its_own_server(self):
        await post_server_notification(self.db, GUILD, "here", "Body")
        self.assertEqual([r["title"] for r in await self.unseen(guild_id=GUILD)], ["here"])
        self.assertEqual(await self.unseen(guild_id=OTHER_GUILD), [])

    async def test_a_server_notice_is_read_once_per_user_per_server(self):
        await post_server_notification(self.db, GUILD, "here", "Body")
        await post_server_notification(self.db, OTHER_GUILD, "there", "Body")
        await self.see(guild_id=GUILD)
        # The other server's notice is a separate feed and still pending.
        self.assertEqual([r["title"] for r in await self.unseen(guild_id=OTHER_GUILD)], ["there"])

    async def test_one_persons_reading_doesnt_mark_it_for_everyone(self):
        await self.add_global("a")
        await self.see(user_id=USER)
        self.assertEqual(len(await self.unseen(user_id=OTHER_USER)), 1)

    async def test_a_dm_still_gets_the_global_feed(self):
        # guild_id is None outside a server. The server subquery matches
        # nothing on its own, so this needs no branch - but it does need to
        # not raise, and the global notice must still arrive.
        await self.add_global("a")
        await post_server_notification(self.db, GUILD, "server", "Body")
        rows = await self.unseen(guild_id=None)
        self.assertEqual([r["scope"] for r in rows], ["global"])

    async def test_the_global_feed_marker_uses_the_sentinel_not_the_guild(self):
        await self.add_global("a")
        await self.see(guild_id=GUILD)
        rows = await self.db.fetchall(
            "SELECT guild_id FROM notification_reads WHERE user_id = ?", (USER,)
        )
        self.assertEqual([r["guild_id"] for r in rows], [GLOBAL_FEED_ID])


class MarkSeenTests(NotificationTestCase):
    async def test_the_marker_only_moves_forward(self):
        # Two commands racing can fetch the same notice and mark it in either
        # order; a slow one committing late must not un-read a newer notice
        # the player has already been shown.
        await self.add_global("a")
        old = await self.unseen()
        await self.add_global("b", title="b")
        await self.see()                      # marks the newer one
        await mark_seen(self.db, USER, old)   # the stale marker lands late
        self.assertEqual(await self.unseen(), [])

    async def test_marking_nothing_is_harmless(self):
        await mark_seen(self.db, USER, [])
        self.assertEqual(
            await self.db.fetchall("SELECT * FROM notification_reads"), []
        )


class SeedingTests(NotificationTestCase):
    async def test_seeding_the_same_release_twice_announces_nothing_new(self):
        # The property that makes it safe to seed on every boot. Without it a
        # bot that restarts nightly re-announces the same disclaimer each
        # morning to everybody.
        notices = (GlobalNotice("a", "A", "Body"),)
        self.assertEqual(await seed_global_notices(self.db, notices), 1)
        self.assertEqual(await seed_global_notices(self.db, notices), 0)
        rows = await self.db.fetchall("SELECT * FROM notifications")
        self.assertEqual(len(rows), 1)

    async def test_a_restart_doesnt_reshow_a_notice_already_read(self):
        notices = (GlobalNotice("a", "A", "Body"),)
        await seed_global_notices(self.db, notices)
        await self.see()
        await seed_global_notices(self.db, notices)
        self.assertEqual(await self.unseen(), [])

    async def test_newest_first_in_the_file_is_newest_shown(self):
        # data/notifications.py lists newest first (matching changelog.py) but
        # what a player sees is the highest id, so seeding has to insert
        # backwards. Only observable when several seed in one pass - i.e. on a
        # fresh database, which is exactly what nobody tests by hand.
        await seed_global_notices(self.db, (
            GlobalNotice("newest", "newest", "Body"),
            GlobalNotice("middle", "middle", "Body"),
            GlobalNotice("oldest", "oldest", "Body"),
        ))
        self.assertEqual([r["title"] for r in await self.unseen()], ["newest"])

    async def test_editing_a_shipped_notice_does_not_rewrite_it(self):
        await seed_global_notices(self.db, (GlobalNotice("a", "Original", "Body"),))
        await seed_global_notices(self.db, (GlobalNotice("a", "Rewritten", "New"),))
        row = await self.db.fetchone("SELECT title, body FROM notifications")
        self.assertEqual((row["title"], row["body"]), ("Original", "Body"))


class ShippedNoticeTests(unittest.TestCase):
    """The notices this release actually ships, checked as data."""

    def test_keys_are_unique(self):
        # A reused key silently swallows the new notice: seeding is INSERT OR
        # IGNORE on it, so the second one is never announced at all.
        keys = [n.key for n in GLOBAL_NOTICES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_notice_has_a_title_and_body(self):
        for notice in GLOBAL_NOTICES:
            self.assertTrue(notice.key, notice)
            self.assertTrue(notice.title, notice.key)
            self.assertTrue(notice.body, notice.key)

    def test_bodies_fit_an_embed_description(self):
        # Discord truncates a description past 4096 characters, and a notice
        # is a single block of prose with nowhere to spill to.
        for notice in GLOBAL_NOTICES:
            self.assertLessEqual(len(notice.body), 4096, notice.key)
            self.assertLessEqual(len(notice.title), 256, notice.key)


class PersonalNoticeTests(NotificationTestCase):
    """Notices raised for one player. The gem hints are the only thing that
    raises one today, and they arrive through adjust_user_quantity rather than
    from a command, which is what these go through the funnel to check."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await ensure_user_row(self.db, USER)
        await ensure_user_row(self.db, OTHER_USER)

    async def personal(self, user_id=USER):
        return [row["notice_key"] for row in await fetch_unseen_personal(self.db, user_id)]

    async def test_a_first_gem_raises_its_notice(self):
        await adjust_user_quantity(self.db, USER, "ruby", 1)
        self.assertEqual(await self.personal(), [GEM_UNLOCK_NOTICES["ruby"].key])

    async def test_an_ordinary_material_raises_nothing(self):
        # The hot path: every /collect credits ore, and none of it announces.
        for material_id in ("iron_ore", "coal", "steel", "iron_drill_bit"):
            await adjust_user_quantity(self.db, USER, material_id, 50)
        self.assertEqual(await self.personal(), [])

    async def test_a_diamond_raises_the_affinity_notice(self):
        # The diamond unlocked no command until /affinity, and this test used
        # to pin that it stayed silent. It opens one now, so it says so - the
        # trigger was always "a gem that opens something", never "a gemstone".
        await adjust_user_quantity(self.db, USER, "diamond", 1)
        self.assertEqual(await self.personal(), [GEM_UNLOCK_NOTICES["diamond"].key])

    async def test_the_second_gem_of_a_kind_says_nothing(self):
        await adjust_user_quantity(self.db, USER, "ruby", 1)
        await mark_personal_seen(self.db, USER, await fetch_unseen_personal(self.db, USER))
        for _ in range(3):
            await adjust_user_quantity(self.db, USER, "ruby", 5)
        self.assertEqual(await self.personal(), [])

    async def test_spending_the_gem_and_finding_another_says_nothing(self):
        """The reason "first" is the notice row and not a 0 -> 1 transition in
        the inventory. Spending the ruby on /focus is the NORMAL thing to do
        with it, and mining another afterwards is common - re-explaining the
        command to somebody already using it is the failure this avoids."""
        await adjust_user_quantity(self.db, USER, "ruby", 1)
        await mark_personal_seen(self.db, USER, await fetch_unseen_personal(self.db, USER))
        await deduct_user_quantity(self.db, USER, "ruby", 1)
        await adjust_user_quantity(self.db, USER, "ruby", 1)
        self.assertEqual(await self.personal(), [])

    async def test_taking_a_gem_away_raises_nothing(self):
        # Nothing calls adjust_user_quantity with a negative delta today, but
        # "you found your first ruby" fired by losing one would be a strange
        # way to hear about it.
        await adjust_user_quantity(self.db, USER, "ruby", -1)
        self.assertEqual(await self.personal(), [])

    async def test_both_gems_are_shown_rather_than_the_newest_only(self):
        """The rule personal notices deliberately break. A broadcast feed shows
        only its newest, because an announcement supersedes the one before it;
        two things that happened to YOU are two separate things to be told."""
        await adjust_user_quantity(self.db, USER, "ruby", 1)
        await adjust_user_quantity(self.db, USER, "obsidian", 1)
        self.assertEqual(
            await self.personal(),
            [GEM_UNLOCK_NOTICES["ruby"].key, GEM_UNLOCK_NOTICES["obsidian"].key],
        )

    async def test_one_players_gem_is_their_own(self):
        await adjust_user_quantity(self.db, USER, "ruby", 1)
        self.assertEqual(await self.personal(OTHER_USER), [])

    async def test_a_notice_is_shown_once_and_not_again(self):
        await adjust_user_quantity(self.db, USER, "ruby", 1)
        rows = await fetch_unseen_personal(self.db, USER)
        await mark_personal_seen(self.db, USER, rows)
        self.assertEqual(await self.personal(), [])

    async def test_marking_it_seen_keeps_the_row_as_the_dedupe_record(self):
        # Seen rows are never deleted - the row IS the record that this player
        # has been told, so removing it would re-notify them on the next gem.
        await adjust_user_quantity(self.db, USER, "ruby", 1)
        await mark_personal_seen(self.db, USER, await fetch_unseen_personal(self.db, USER))
        row = await self.db.fetchone(
            "SELECT seen_at FROM user_notifications WHERE user_id = ?", (USER,)
        )
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["seen_at"])

    async def test_posting_reports_whether_it_was_new(self):
        self.assertTrue(await post_user_notification(self.db, USER, "k", "T", "B"))
        self.assertFalse(await post_user_notification(self.db, USER, "k", "T", "B"))

    async def test_announce_first_gem_reports_the_same(self):
        self.assertTrue(await announce_first_gem(self.db, USER, "ruby"))
        self.assertFalse(await announce_first_gem(self.db, USER, "ruby"))
        self.assertFalse(await announce_first_gem(self.db, USER, "iron_ore"))


class RewrittenNoticeTests(NotificationTestCase):
    """set_user_notification: a personal notice kept open and rewritten until
    the player sees it, and optionally shown in one server only. Repaid bonds
    are what use it (tests/test_government.py: RepaidBondNoticeTests)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await ensure_user_row(self.db, USER)

    async def bodies(self, guild_id=GUILD):
        return [row["body"] for row in await fetch_unseen_personal(self.db, USER, guild_id)]

    async def test_an_unread_notice_is_rewritten_in_place(self):
        await set_user_notification(self.db, USER, "k", "T", "one")
        await set_user_notification(self.db, USER, "k", "T2", "two")
        rows = await fetch_unseen_personal(self.db, USER)
        self.assertEqual([(row["title"], row["body"]) for row in rows], [("T2", "two")])

    async def test_a_read_notice_keeps_what_the_player_was_shown(self):
        await set_user_notification(self.db, USER, "k", "T", "one")
        await mark_personal_seen(self.db, USER, await fetch_unseen_personal(self.db, USER))
        await set_user_notification(self.db, USER, "k", "T", "two")
        self.assertEqual(await self.bodies(), [])
        row = await self.db.fetchone("SELECT body FROM user_notifications WHERE notice_key = 'k'")
        self.assertEqual(row["body"], "one")

    async def test_a_notice_rewritten_while_the_reply_was_in_flight_stays_unread(self):
        """respond() fetches, sends, then marks. Marking the version it fetched
        must not close the newer one, or the newest total is never shown."""
        await set_user_notification(self.db, USER, "k", "T", "one")
        shown = await fetch_unseen_personal(self.db, USER)
        await set_user_notification(self.db, USER, "k", "T", "two")
        await mark_personal_seen(self.db, USER, shown)
        self.assertEqual(await self.bodies(), ["two"])

    async def test_a_server_notice_is_shown_only_in_that_server(self):
        await set_user_notification(self.db, USER, "k", "T", "here", guild_id=GUILD)
        self.assertEqual(await self.bodies(GUILD), ["here"])
        self.assertEqual(await self.bodies(OTHER_GUILD), [])
        self.assertEqual(await self.bodies(None), [], "nor in a DM")

    async def test_a_notice_with_no_server_is_shown_anywhere(self):
        await post_user_notification(self.db, USER, "k", "T", "anywhere")
        for guild_id in (GUILD, OTHER_GUILD, None):
            with self.subTest(guild_id=guild_id):
                self.assertEqual(await self.bodies(guild_id), ["anywhere"])

    async def test_respond_delivers_it_only_in_its_server(self):
        await set_user_notification(self.db, USER, "k", "Bonds", "here", guild_id=GUILD)
        elsewhere = _FakeInteraction(guild_id=OTHER_GUILD)
        await respond(elsewhere, self.db, embed=discord.Embed(title="command"))
        self.assertEqual([e.title for e in elsewhere.embeds], ["command"])
        here = _FakeInteraction(guild_id=GUILD)
        await respond(here, self.db, embed=discord.Embed(title="command"))
        self.assertEqual([e.title for e in here.embeds], ["command", "Bonds"])
        self.assertEqual(await self.bodies(GUILD), [])


class GemUnlockNoticeTests(unittest.TestCase):
    """The three hints as data. Their wording is meant to be edited freely - what
    these check is the handful of things the rest of the system relies on."""

    def test_they_cover_exactly_the_gems_that_unlock_a_command(self):
        self.assertEqual(set(GEM_UNLOCK_NOTICES), {"ruby", "obsidian", "diamond"})

    def test_keys_are_unique(self):
        # Two notices sharing a key means the second never fires: the primary
        # key on user_notifications is (user_id, notice_key), so posting it is
        # a no-op for anyone who already has the first.
        keys = [n.key for n in GEM_UNLOCK_NOTICES.values()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_no_key_collides_with_a_shipped_announcement(self):
        # Different tables, so this could not actually break anything - but a
        # key meaning two things in two places is how it starts.
        self.assertFalse(
            {n.key for n in GEM_UNLOCK_NOTICES.values()} & {n.key for n in GLOBAL_NOTICES}
        )

    def test_each_names_the_command_it_is_about(self):
        # The entire point of the notice. Everything else in it is flavour.
        self.assertIn("/focus", GEM_UNLOCK_NOTICES["ruby"].body)
        self.assertIn("/efficiency", GEM_UNLOCK_NOTICES["obsidian"].body)
        self.assertIn("/affinity", GEM_UNLOCK_NOTICES["diamond"].body)

    def test_each_still_costs_the_one_gem_the_text_promises(self):
        # The bodies say the unlock is paid once, in the singular. If either
        # cost stops being a single gem of that type, the wording is wrong.
        self.assertEqual(MINING_FOCUS_UNLOCK_COST, {"ruby": 1})
        self.assertEqual(MINING_EFFICIENCY_UNLOCK_COST, {"obsidian": 1})
        self.assertEqual(MINING_AFFINITY_UNLOCK_COST, {"diamond": 1})

    def test_they_fit_an_embed(self):
        for notice in GEM_UNLOCK_NOTICES.values():
            self.assertTrue(notice.title, notice.key)
            self.assertTrue(notice.body, notice.key)
            self.assertLessEqual(len(notice.body), 4096, notice.key)
            self.assertLessEqual(len(notice.title), 256, notice.key)


class MergeEmbedsTests(unittest.TestCase):
    """Folding notices onto a reply that may already carry an embed.

    Worth its own tests because send_message rejects `embed` and `embeds`
    together, so the failure mode is a TypeError raised from inside whatever
    command happened to run first after an announcement went out - a long way
    from anything that looks like notification code.
    """

    @staticmethod
    def merged(**kwargs):
        extra = [discord.Embed(title="notice")]
        _merge_embeds(kwargs, extra)
        return kwargs

    def test_a_content_only_reply_gains_an_embeds_list(self):
        result = self.merged(content="Sold 5x Iron Ore.")
        self.assertEqual(result["content"], "Sold 5x Iron Ore.")
        self.assertEqual([e.title for e in result["embeds"]], ["notice"])
        self.assertNotIn("embed", result)

    def test_a_single_embed_becomes_a_list_with_the_notice_after_it(self):
        result = self.merged(embed=discord.Embed(title="command"))
        self.assertNotIn("embed", result)
        self.assertEqual([e.title for e in result["embeds"]], ["command", "notice"])

    def test_other_kwargs_are_left_alone(self):
        sentinel = object()
        result = self.merged(embed=discord.Embed(title="command"), view=sentinel)
        self.assertIs(result["view"], sentinel)

    def test_an_existing_embeds_list_is_appended_to(self):
        result = self.merged(embeds=[discord.Embed(title="a"), discord.Embed(title="b")])
        self.assertEqual([e.title for e in result["embeds"]], ["a", "b", "notice"])


class NoticeEmbedTests(NotificationTestCase):
    async def test_every_kind_of_notice_renders_in_its_own_color(self):
        """All three can land in one message (utils/responses.py merges them
        onto the reply), so they have to be distinguishable from each other,
        not merely from the reply."""
        await ensure_user_row(self.db, USER)
        await self.add_global("a", title="global")
        await post_server_notification(self.db, GUILD, "server", "Body")
        await adjust_user_quantity(self.db, USER, "ruby", 1)
        rows = [*await self.unseen(), *await fetch_unseen_personal(self.db, USER)]
        embeds = [notice_embed(row) for row in rows]
        self.assertEqual(len(embeds), 3)
        self.assertEqual(len({e.color for e in embeds}), 3)
        self.assertEqual(len({e.author.name for e in embeds}), 3)

    async def test_the_embed_carries_the_notice_text(self):
        await self.add_global("a", title="Heads up", body="Something happened.")
        embed = notice_embed((await self.unseen())[0])
        self.assertEqual(embed.title, "Heads up")
        self.assertEqual(embed.description, "Something happened.")


class _StrictResponse:
    """interaction.response, refusing what Discord refuses. A mock that took
    anything is how attaching every pending notice at once got through: the
    10-embed cap is enforced by discord.py and the character total by the API,
    and neither runs in a test."""

    def __init__(self):
        self.send_message = AsyncMock(side_effect=self._check)

    @staticmethod
    async def _check(*args, embeds=(), **kwargs):
        if len(embeds) > MAX_EMBEDS_PER_MESSAGE:
            raise ValueError("embeds has a maximum of 10 elements.")
        if sum(len(e) for e in embeds) > MAX_EMBED_CHARS_PER_MESSAGE:
            raise ValueError("embed size exceeds maximum size of 6000")


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class _FakeInteraction:
    def __init__(self, user_id=USER, guild_id=GUILD):
        self.guild_id = guild_id
        self.user = _FakeUser(user_id)
        self.response = _StrictResponse()

    @property
    def embeds(self):
        """Everything the reply carried, whichever key it went under: a reply
        with no notice to attach keeps the command's own `embed=`."""
        kwargs = self.response.send_message.call_args.kwargs
        single = kwargs.get("embed")
        return [single] if single is not None else list(kwargs.get("embeds") or [])


class OverflowTests(NotificationTestCase):
    """More notices pending than one reply can carry.

    The incident this pins: a player owed a dozen personal notices had every
    reply refused by discord.py, and since notices are only marked seen after a
    successful send, no command of theirs answered again. The rule is that the
    command's own answer always goes, notices fill what room is left, and the
    rest arrive on later commands."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await ensure_user_row(self.db, USER)

    async def owe(self, count, body="Body"):
        for n in range(count):
            await post_user_notification(self.db, USER, f"k{n}", f"notice {n}", body)

    async def reply(self, **kwargs):
        interaction = _FakeInteraction()
        kwargs.setdefault("embed", discord.Embed(title="command"))
        await respond(interaction, self.db, **kwargs)
        return [e.title for e in interaction.embeds]

    async def test_a_backlog_beyond_ten_embeds_still_gets_an_answer(self):
        await self.owe(12)
        titles = await self.reply()
        self.assertEqual(len(titles), MAX_EMBEDS_PER_MESSAGE)
        self.assertEqual(titles[0], "command")

    async def test_the_backlog_drains_over_later_commands_in_order(self):
        await self.owe(12)
        first = await self.reply()
        second = await self.reply()
        third = await self.reply()
        shown = first[1:] + second[1:] + third[1:]
        self.assertEqual(shown, [f"notice {n}" for n in range(12)])
        self.assertEqual(third, ["command"])

    async def test_a_command_sending_several_embeds_leaves_less_room(self):
        await self.owe(12)
        own = [discord.Embed(title=f"page {n}") for n in range(4)]
        titles = await self.reply(embed=None, embeds=own)
        self.assertEqual(titles[:4], ["page 0", "page 1", "page 2", "page 3"])
        self.assertEqual(len(titles), MAX_EMBEDS_PER_MESSAGE)
        self.assertEqual(len(await fetch_unseen_personal(self.db, USER)), 6)

    async def test_long_notices_are_held_to_the_character_total(self):
        # Two 4000-character bodies are under the embed count by a mile and
        # over the 6000-character total together.
        await self.owe(2, body="x" * 4000)
        self.assertEqual(await self.reply(), ["command", "notice 0"])
        self.assertEqual(await self.reply(), ["command", "notice 1"])

    async def test_broadcasts_still_go_first_and_are_marked_once_shown(self):
        await self.add_global("a", title="global")
        await post_server_notification(self.db, GUILD, "server", "Body")
        await self.owe(12)
        titles = await self.reply()
        self.assertEqual(titles[1:3], ["global", "server"])
        self.assertEqual(await self.unseen(), [])

    async def test_a_broadcast_that_did_not_fit_is_not_marked_seen(self):
        await self.add_global("a", title="global")
        own = [discord.Embed(title=f"page {n}") for n in range(MAX_EMBEDS_PER_MESSAGE)]
        await self.reply(embed=None, embeds=own)
        self.assertEqual([row["title"] for row in await self.unseen()], ["global"])


if __name__ == "__main__":
    unittest.main()
