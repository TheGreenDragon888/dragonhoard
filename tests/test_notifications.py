"""
Tests for the notification system against a real database.

The rules worth pinning are the ones a player would notice going wrong: a
notice is shown once and not again, only the NEWEST of each feed is shown, the
global feed is per-user while the server feed is per-user-per-guild, and seeding
the same release twice doesn't re-announce anything.
"""
import tempfile
import unittest
from pathlib import Path

import discord

from database.db import Database
from data.notifications import GLOBAL_NOTICES, GlobalNotice
from utils.responses import _merge_embeds
from utils.notifications import (
    GLOBAL_FEED_ID,
    fetch_unseen,
    mark_seen,
    notice_embed,
    post_server_notification,
    seed_global_notices,
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
    async def test_the_two_feeds_render_in_different_colors(self):
        await self.add_global("a", title="global")
        await post_server_notification(self.db, GUILD, "server", "Body")
        embeds = [notice_embed(row) for row in await self.unseen()]
        self.assertEqual(len(embeds), 2)
        self.assertNotEqual(embeds[0].color, embeds[1].color)

    async def test_the_embed_carries_the_notice_text(self):
        await self.add_global("a", title="Heads up", body="Something happened.")
        embed = notice_embed((await self.unseen())[0])
        self.assertEqual(embed.title, "Heads up")
        self.assertEqual(embed.description, "Something happened.")


if __name__ == "__main__":
    unittest.main()
