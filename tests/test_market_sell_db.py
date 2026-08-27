"""
Tests for /market sell's receipt against a real database, specifically the
case where the sale also finishes today's job board task.

The "Received" field's bolded amount has to be the full amount that landed in
the user's balance - sale value plus job board bonus - not just the sale's
own half of it. balance_after already reflected both credits (they commit in
the same transaction); this pins the bolded amount to match it.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from discord import app_commands

from cogs.economy import EconomyCog, TRADEABLE_MATERIALS
from database.db import Database
from utils.db_helpers import ensure_server_row, ensure_user_row, adjust_user_quantity, get_currency_balance
from utils.formatting import format_price
from utils.job_board import ensure_todays_job
from data.materials import sale_unit_price

GUILD = 3131
USER = 2121
MEMBERS = 10


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = "Tester"


class FakeGuild:
    """Just enough of discord.Guild for human_member_count: already chunked,
    no members, so the member count used to size the job board is 0 - fine,
    since the job is posted ahead of time in these tests regardless."""
    chunked = True
    members = []

    async def chunk(self):
        pass


class FakeInteraction:
    def __init__(self, guild_id, user_id):
        self.guild_id = guild_id
        self.guild = FakeGuild()
        self.user = FakeUser(user_id)
        self.response = AsyncMock()


class MarketSellJobBoardReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)

        self.cog = EconomyCog.__new__(EconomyCog)
        self.cog.db = self.db

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def sell(self, laps):
        """Posts today's job, hands the user `laps` times the task quantity of
        its material, sells the lot in one command, and returns the receipt
        embed alongside what actually landed in their balance."""
        async with self.db.transaction() as tx:
            job = await ensure_todays_job(tx, GUILD, MEMBERS)
        quantity = job["quantity"] * laps
        await adjust_user_quantity(self.db, USER, job["material_id"], quantity)

        balance_before = await get_currency_balance(self.db, GUILD, USER)

        material = app_commands.Choice(
            name=TRADEABLE_MATERIALS[job["material_id"]]["name"], value=job["material_id"]
        )
        interaction = FakeInteraction(GUILD, USER)
        # market_sell is wrapped in an app_commands.Command by the decorator,
        # so the cog instance has to be passed to the raw callback by hand
        # rather than called as a bound method.
        await EconomyCog.market_sell.callback(self.cog, interaction, material, quantity)

        credited = await get_currency_balance(self.db, GUILD, USER) - balance_before
        kwargs = interaction.response.send_message.call_args.kwargs
        embed = kwargs["embeds"][0] if "embeds" in kwargs else kwargs["embed"]
        return job, embed, credited

    async def test_received_includes_the_job_board_bonus(self):
        _, embed, credited = await self.sell(1)
        # The sale alone can't have completed a job AND paid nothing for it.
        self.assertGreater(credited, 0)

        received = next(f.value for f in embed.fields if f.name == "Received")
        # The BOLDED amount specifically - the parenthetical "remaining"
        # figure is correct even when the bug this test guards is present
        # (it's read fresh from the balance), so asserting against the whole
        # field string would pass either way.
        self.assertIn(f"**{format_price(credited, round_up=False)}**", received)
        self.assertIn("job board", embed.description)
        # One completion reads as a plain sentence, with no count in it.
        self.assertNotIn("times", embed.description)

    async def test_a_sale_worth_several_completions_pays_and_says_so(self):
        """The 1.3 behaviour end to end: one command, several completions, one
        receipt. The bonus is the flat payout per completion, and "Received"
        still has to be everything that landed."""
        job, embed, credited = await self.sell(4)
        sale = job["quantity"] * 4 * sale_unit_price(job["material_id"])
        self.assertAlmostEqual(credited, sale + job["reward"] * 4)

        received = next(f.value for f in embed.fields if f.name == "Received")
        self.assertIn(f"**{format_price(credited, round_up=False)}**", received)
        self.assertIn("**4** times", embed.description)


if __name__ == "__main__":
    unittest.main()
