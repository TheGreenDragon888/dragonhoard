"""
cogs/shares.py

Implements server shares (1.6):
  - /shares status                     - this server's shares, sale, best prices
  - /shares buy <quantity>             - from players' listings and the government's sale
  - /shares sell <quantity>            - into players' bids
  - /shares list <quantity> <price>    - offer yours to other members
  - /shares order <quantity> <price>   - bid for shares, DragonCoin held
  - /shares cancel <entry>             - withdraw one of your listings or orders
  - /shares holdings                   - your shares in every server

The Treasurer's and Mayor's side (/treasurer shares, /mayor shares, /mayor
buyback) is in cogs/government.py, with the rest of their commands. Every rule
and every currency movement is utils/shares.py; the design is docs/shares.md.

Every command but /shares holdings trades the shares of the server it is run
in, with no server parameter, at the owner's rule that you may only trade
shares in a server you are in - Discord lets only members run a command there,
so that is the check.

THE HOURLY LOOP pays dividends (servers with revenue set aside) and clears
expired share entries (servers with any), each server guarded on its own as the
government loop is.

The government's sky blue (docs/stylization.md): shares are sold by the
government, the way bonds are.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.db import InsufficientQuantity
from utils.embeds import GOVERNMENT_COLOR, add_multi_field, footer_with, make_embed
from utils.exchange import dragoncoin_cents, get_dragoncoin_cents
from utils.formatting import format_currency, format_dragoncoin, plural
from utils.government import SHARE_REVENUE_PERCENT, GovernmentError
from utils.notifications import post_user_notification
from utils.receipts import build_action_receipt, dragoncoin_line
from utils.responses import respond
from utils.shares import (
    GOVERNMENT,
    LISTING,
    ORDER,
    available_shares,
    buy_shares,
    cancel_share_entry,
    expire_share_entries,
    guilds_with_dividends,
    guilds_with_expired_share_entries,
    held_shares,
    holdings_of,
    list_shares,
    order_shares,
    own_share_entries,
    pay_dividends,
    sell_shares,
    share_status,
)
from utils.market_book import MARKET_ENTRY_LIFETIME_DAYS

log = logging.getLogger("dragonhoard")

# Unicode for titles and author lines, which render no custom emoji.
SHARES_EMOJI = "\U0001F4C8"

NOT_IN_A_SERVER = "Shares belong to a server. Use this in the server whose shares you want."
RACE_MESSAGE = "Something changed while that was going through - nothing was traded. Try again."

# The most holdings /shares holdings lists before summing up the rest, for the
# embed-size reason cogs/government.py: BOND_LINES_SHOWN gives.
HOLDING_LINES_SHOWN = 20


def _fill_lines(fills) -> list[str]:
    """Who a trade was with, one line each, for a receipt's description."""
    lines = []
    for fill in fills:
        who = "the government" if fill.source == GOVERNMENT else f"<@{fill.counterparty}>"
        lines.append(f"· **{fill.quantity:,}** with {who} at {format_dragoncoin(fill.price_cents)} each")
    return lines


def _shares_line(quantity: int, after: int, *, gained: bool) -> str:
    return f"{SHARES_EMOJI} **{quantity:,}** ({after:,} {'total' if gained else 'remaining'})"


class SharesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.shares_loop.start()

    def cog_unload(self):
        self.shares_loop.cancel()

    shares_group = app_commands.Group(name="shares", description="Own a slice of this server's revenue, bought with DragonCoin")

    async def _refuse(self, interaction: discord.Interaction, message: str):
        await interaction.response.send_message(message, ephemeral=True)

    async def _price(self, interaction: discord.Interaction, price: float) -> int | None:
        """A price typed in DragonCoin, in hundredths; refuses one that rounds
        to nothing."""
        cents = dragoncoin_cents(price)
        if cents < 1:
            await self._refuse(interaction, f"The lowest price is {format_dragoncoin(1)} a share.")
            return None
        return cents

    # -----------------------------------------------------------------------
    # /shares status
    # -----------------------------------------------------------------------

    @shares_group.command(name="status", description="This server's shares: how many, the sale, prices and yours")
    async def shares_status(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            await self._refuse(interaction, NOT_IN_A_SERVER)
            return
        guild_id, user_id = interaction.guild_id, interaction.user.id
        status = await share_status(self.db, guild_id)
        emoji = status.currency_emoji
        held = await held_shares(self.db, guild_id, user_id)
        free = await available_shares(self.db, guild_id, user_id)
        listings, orders = await own_share_entries(self.db, guild_id, user_id)

        embed = make_embed(
            f"{status.outstanding:,} of {status.ceiling:,} shares · {SHARE_REVENUE_PERCENT:g}% of revenue each",
            GOVERNMENT_COLOR,
        )
        if interaction.guild is not None:
            embed.set_author(name=f"{SHARES_EMOJI} Shares • {interaction.guild.name}")
        if status.sale_quantity:
            embed.description = (
                f"**Government sale:** {status.sale_quantity:,} at "
                f"{format_dragoncoin(status.sale_price_cents)} each · `/shares buy`"
            )
        else:
            embed.description = "No government sale right now."

        per_share = status.week_revenue * SHARE_REVENUE_PERCENT / 100
        embed.add_field(
            name="Revenue · 7d",
            value=f"{format_currency(status.week_revenue, emoji)}\n"
                  f"{format_currency(per_share, emoji)} per share",
            inline=True,
        )
        embed.add_field(
            name="Best price",
            value=(
                f"Ask {format_dragoncoin(status.best_ask_cents) if status.best_ask_cents else '—'}\n"
                f"Bid {format_dragoncoin(status.best_bid_cents) if status.best_bid_cents else '—'}"
            ),
            inline=True,
        )
        embed.add_field(name="Price floor", value=format_dragoncoin(status.floor_cents), inline=True)
        embed.add_field(
            name="You hold",
            value=f"**{held:,}** {plural('share', held)}" + (f" · {held - free:,} listed" if held != free else ""),
            inline=True,
        )
        entry_lines = [
            f"`L{row['listing_id']}` selling **{row['quantity']:,}** at {format_dragoncoin(row['price_cents'])}"
            for row in listings
        ] + [
            f"`O{row['order_id']}` buying **{row['quantity']:,}** at {format_dragoncoin(row['price_cents'])}"
            for row in orders
        ]
        if entry_lines:
            add_multi_field(embed, "Your listings and orders", entry_lines)
        embed.set_footer(text=footer_with("/help shares"))
        await respond(interaction, self.db, embed=embed)

    # -----------------------------------------------------------------------
    # /shares buy and /shares sell
    # -----------------------------------------------------------------------

    @shares_group.command(name="buy", description="Buy this server's shares, cheapest first")
    @app_commands.describe(quantity="How many shares")
    async def shares_buy(self, interaction: discord.Interaction, quantity: app_commands.Range[int, 1]):
        if interaction.guild_id is None:
            await self._refuse(interaction, NOT_IN_A_SERVER)
            return
        try:
            async with self.db.transaction() as tx:
                trade = await buy_shares(tx, interaction.guild_id, interaction.user.id, quantity)
        except GovernmentError as exc:
            await self._refuse(interaction, str(exc))
            return
        except InsufficientQuantity:
            await self._refuse(interaction, RACE_MESSAGE)
            return
        embed = build_action_receipt(
            f"{SHARES_EMOJI} Shares Bought", GOVERNMENT_COLOR,
            "\n".join([f"Bought **{quantity:,}** {plural('share', quantity)} of this server."] + _fill_lines(trade.fills)),
            [
                ("Spent", dragoncoin_line(trade.total_cents, trade.dragoncoin_after, gained=False)),
                ("Shares", _shares_line(quantity, trade.shares_after, gained=True)),
            ],
            footer_note="dividends are paid hourly in DragonCoin",
        )
        await respond(interaction, self.db, embed=embed)

    @shares_group.command(name="sell", description="Sell your shares here to the highest bids")
    @app_commands.describe(quantity="How many shares")
    async def shares_sell(self, interaction: discord.Interaction, quantity: app_commands.Range[int, 1]):
        if interaction.guild_id is None:
            await self._refuse(interaction, NOT_IN_A_SERVER)
            return
        try:
            async with self.db.transaction() as tx:
                trade = await sell_shares(tx, interaction.guild_id, interaction.user.id, quantity)
        except GovernmentError as exc:
            await self._refuse(interaction, str(exc))
            return
        except InsufficientQuantity:
            await self._refuse(interaction, RACE_MESSAGE)
            return
        embed = build_action_receipt(
            f"{SHARES_EMOJI} Shares Sold", GOVERNMENT_COLOR,
            "\n".join([f"Sold **{quantity:,}** {plural('share', quantity)} of this server."] + _fill_lines(trade.fills)),
            [
                ("Sold", _shares_line(quantity, trade.shares_after, gained=False)),
                ("Received", dragoncoin_line(trade.total_cents, trade.dragoncoin_after, gained=True)),
            ],
        )
        await respond(interaction, self.db, embed=embed)

    # -----------------------------------------------------------------------
    # /shares list, /shares order, /shares cancel
    # -----------------------------------------------------------------------

    @shares_group.command(name="list", description="Offer your shares here at your price")
    @app_commands.describe(quantity="How many shares", price="DragonCoin per share, to the nearest 0.01")
    async def shares_list(
        self, interaction: discord.Interaction, quantity: app_commands.Range[int, 1], price: float,
    ):
        if interaction.guild_id is None:
            await self._refuse(interaction, NOT_IN_A_SERVER)
            return
        price_cents = await self._price(interaction, price)
        if price_cents is None:
            return
        try:
            async with self.db.transaction() as tx:
                listing_id = await list_shares(tx, interaction.guild_id, interaction.user.id, quantity, price_cents)
        except GovernmentError as exc:
            await self._refuse(interaction, str(exc))
            return
        embed = build_action_receipt(
            f"{SHARES_EMOJI} Shares Listed", GOVERNMENT_COLOR,
            f"Listed **{quantity:,}** {plural('share', quantity)} at {format_dragoncoin(price_cents)} each as `L{listing_id}`. "
            f"They keep earning until sold, for up to {MARKET_ENTRY_LIFETIME_DAYS} days.",
            [],
            footer_note="/shares cancel takes it back",
        )
        await respond(interaction, self.db, embed=embed)

    @shares_group.command(name="order", description="Bid for this server's shares at your price")
    @app_commands.describe(quantity="How many shares", price="DragonCoin per share, to the nearest 0.01")
    async def shares_order(
        self, interaction: discord.Interaction, quantity: app_commands.Range[int, 1], price: float,
    ):
        if interaction.guild_id is None:
            await self._refuse(interaction, NOT_IN_A_SERVER)
            return
        price_cents = await self._price(interaction, price)
        if price_cents is None:
            return
        try:
            async with self.db.transaction() as tx:
                order_id = await order_shares(tx, interaction.guild_id, interaction.user.id, quantity, price_cents)
                after = await get_dragoncoin_cents(tx, interaction.user.id)
        except GovernmentError as exc:
            await self._refuse(interaction, str(exc))
            return
        embed = build_action_receipt(
            f"{SHARES_EMOJI} Order Placed", GOVERNMENT_COLOR,
            f"Bidding for **{quantity:,}** {plural('share', quantity)} at {format_dragoncoin(price_cents)} each as `O{order_id}`, "
            f"for up to {MARKET_ENTRY_LIFETIME_DAYS} days.",
            [("Held", dragoncoin_line(quantity * price_cents, after, gained=False))],
            footer_note="/shares cancel returns what it holds",
        )
        await respond(interaction, self.db, embed=embed)

    async def _entry_choices(self, interaction: discord.Interaction, current: str):
        if interaction.guild_id is None:
            return []
        listings, orders = await own_share_entries(self.db, interaction.guild_id, interaction.user.id)
        choices = [
            app_commands.Choice(
                name=f"L{row['listing_id']} · selling {row['quantity']:,} at {row['price_cents'] / 100:.2f}",
                value=f"{LISTING}:{row['listing_id']}",
            )
            for row in listings
        ] + [
            app_commands.Choice(
                name=f"O{row['order_id']} · buying {row['quantity']:,} at {row['price_cents'] / 100:.2f}",
                value=f"{ORDER}:{row['order_id']}",
            )
            for row in orders
        ]
        needle = current.lower()
        return [choice for choice in choices if needle in choice.name.lower()][:25]

    @shares_group.command(name="cancel", description="Withdraw one of your share listings or orders here")
    @app_commands.describe(entry="Which listing or order")
    @app_commands.autocomplete(entry=_entry_choices)
    async def shares_cancel(self, interaction: discord.Interaction, entry: str):
        if interaction.guild_id is None:
            await self._refuse(interaction, NOT_IN_A_SERVER)
            return
        kind, _, raw_id = entry.partition(":")
        if kind not in (LISTING, ORDER) or not raw_id.isdigit():
            await self._refuse(interaction, "Pick one of your listings or orders from the list.")
            return
        try:
            async with self.db.transaction() as tx:
                row, refund = await cancel_share_entry(
                    tx, interaction.guild_id, interaction.user.id, kind, int(raw_id)
                )
                after = await get_dragoncoin_cents(tx, interaction.user.id)
        except GovernmentError as exc:
            await self._refuse(interaction, str(exc))
            return
        what = "listing" if kind == LISTING else "order"
        fields = [("Returned", dragoncoin_line(refund, after, gained=True))] if refund else []
        embed = build_action_receipt(
            f"{SHARES_EMOJI} {what.title()} Withdrawn", GOVERNMENT_COLOR,
            f"Withdrew your {what} for **{row['quantity']:,}** {plural('share', row['quantity'])} at "
            f"{format_dragoncoin(row['price_cents'])} each.",
            fields,
        )
        await respond(interaction, self.db, embed=embed)

    # -----------------------------------------------------------------------
    # /shares holdings
    # -----------------------------------------------------------------------

    @shares_group.command(name="holdings", description="Your shares in every server, and the dividends they've paid")
    async def shares_holdings(self, interaction: discord.Interaction):
        rows = await holdings_of(self.db, interaction.user.id)
        total = sum(row["shares"] for row in rows)
        embed = make_embed(
            f"{total:,} {plural('share', total)} in {len(rows):,} {plural('server', len(rows))}" if rows
            else "No shares yet",
            GOVERNMENT_COLOR,
        )
        embed.set_author(name=f"{SHARES_EMOJI} {interaction.user.display_name}'s Shares")
        if rows:
            lines = []
            for row in rows[:HOLDING_LINES_SHOWN]:
                guild = self.bot.get_guild(row["guild_id"]) if self.bot else None
                name = guild.name if guild else (row["currency_name"] or f"Server {row['guild_id']}")
                lines.append(
                    f"**{name}** · {row['shares']:,} {plural('share', row['shares'])} · "
                    f"{format_currency(row['accrued'], row['currency_emoji'])} waiting · "
                    f"{format_dragoncoin(row['dividends_paid_cents'])} paid"
                )
            if len(rows) > HOLDING_LINES_SHOWN:
                lines.append(f"and {len(rows) - HOLDING_LINES_SHOWN} more")
            add_multi_field(embed, "Holdings", lines)
        embed.set_footer(text=footer_with("dividends turn into DragonCoin each 0.01"))
        await respond(interaction, self.db, embed=embed)

    # -----------------------------------------------------------------------
    # The hourly loop
    # -----------------------------------------------------------------------

    @tasks.loop(hours=1)
    async def shares_loop(self):
        for guild_id in await guilds_with_dividends(self.db):
            await self._guarded("paying dividends", guild_id, self._pay, guild_id)
        for guild_id in await guilds_with_expired_share_entries(self.db):
            await self._guarded("expiring share entries", guild_id, self._expire, guild_id)

    async def _guarded(self, what: str, guild_id: int, step, *args):
        try:
            await step(*args)
        except Exception:
            log.exception("Shares loop: %s in guild %s failed.", what, guild_id)

    async def _pay(self, guild_id: int):
        async with self.db.transaction() as tx:
            await pay_dividends(tx, guild_id)

    async def _expire(self, guild_id: int):
        """One server's sweep, with one personal notice per owner naming what
        ended - committed with the sweep, so it never describes one that rolled
        back."""
        async with self.db.transaction() as tx:
            listings, orders = await expire_share_entries(tx, guild_id)
            lines: dict[int, list[str]] = {}
            keys: dict[int, str] = {}
            for row in listings:
                keys.setdefault(row["seller_id"], f"share_expired:listing:{row['listing_id']}")
                lines.setdefault(row["seller_id"], []).append(
                    f"Your listing of **{row['quantity']:,}** {plural('share', row['quantity'])} ended. You still have them."
                )
            for row, refund in orders:
                keys.setdefault(row["buyer_id"], f"share_expired:order:{row['order_id']}")
                lines.setdefault(row["buyer_id"], []).append(
                    f"Your bid for **{row['quantity']:,}** {plural('share', row['quantity'])} ended: {format_dragoncoin(refund)} back."
                )
            for owner, ended in lines.items():
                await post_user_notification(
                    tx, owner, keys[owner], "⏳ Share entries expired", "\n".join(ended),
                )

    @shares_loop.before_loop
    async def before_shares_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(SharesCog(bot))
