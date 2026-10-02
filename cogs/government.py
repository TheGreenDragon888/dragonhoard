"""
cogs/government.py

Implements the server government (1.4):
  - /vote mayor <member>, /vote treasurer <member>  - Thursdays only
  - /government status                             - who holds office, the
                                                     settings, the treasury,
                                                     the debt and the projects
  - /treasurer fee <machine> <multiplier>          - Treasurer only
  - /treasurer vat <fee|market> <percent>
  - /treasurer bondrate <percent>
  - /mayor fund <machine> <amount>                 - Mayor only
  - /mayor enhance <machine>
  - /mayor slots <amount>
  - /mayor bonanza
  - /mayor bonds <amount>                          - open (or, at 0, withdraw) a bond sale
  - /bonds buy <denomination>, /bonds holdings

Every rule and every currency movement lives in utils/government.py; this
file decides who may ask, how the answer looks, and when the background work
runs. The design, and the reasoning behind every number, is docs/government.md.

ADMINS HAVE NO SAY IN ANY OF IT. That was the design: /setup fee was removed so
the fees are the Treasurer's alone, and nothing here checks Manage Server.

THE HOURLY LOOP does four things, each touching only the servers it has to:
announces Thursday's vote (on Thursdays, to servers not yet told), counts a
finished vote (servers with votes waiting), pays bondholders (servers with a
non-empty repayment pool), and checks whether any frozen creditor has come
back while the bot was not watching. A finished vote is ALSO counted lazily by
any government command, so nobody acts on last week's result in the hour
after midnight.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.db import InsufficientQuantity
from data.materials import (
    BLAST_FURNACE_BATCH_SIZE,
    BONANZA_HOURS,
    MINING_SLOT_ENHANCEMENT_MULTIPLIER,
    enhancement_price,
    enhancement_speed,
)
from utils.betting import hours_until
from utils.db_helpers import (
    MACHINES,
    ensure_server_row,
    get_currency_balance,
    machine_fee,
    mining_slot_status,
)
from utils.embeds import GOVERNMENT_COLOR, MACHINE_DISPLAY, add_multi_field, machine_display, make_embed
from utils.formatting import (
    DEFAULT_CURRENCY_EMOJI,
    format_currency,
    format_exact_currency,
    format_exact_price,
    format_price,
    format_relative_timestamp,
)
from utils.receipts import build_action_receipt, currency_line
from utils.government import (
    BOND_DENOMINATIONS_CENTS,
    FEE_MULTIPLIERS,
    MAX_BOND_RATE_PERCENT,
    FEE_VAT,
    MAYOR,
    OFFICE_LABELS,
    TREASURER,
    VAT_KINDS,
    VAT_LABELS,
    VAT_PERCENTS,
    GovernmentError,
    announce_voting,
    bonanza_quote,
    bonds_held,
    buy_bond,
    buy_enhancement,
    cast_vote,
    count_election,
    frozen_holders,
    fund_machine,
    format_vat,
    fund_mining_slots,
    game_date,
    game_midnight,
    government_status,
    guilds_with_due_votes,
    guilds_with_repayments,
    member_left,
    member_returned,
    next_voting_day,
    open_bond_sale,
    pay_bondholders,
    prune_tax_history,
    set_bond_rate,
    set_fee_multiplier,
    set_vat,
    start_bonanza,
    voting_open,
)
from utils.responses import respond

log = logging.getLogger("dragonhoard")

MACHINE_CHOICES = [
    app_commands.Choice(name=MACHINE_DISPLAY[machine][1], value=machine) for machine in MACHINES
]
# Strings rather than floats on the wire, so the value that reaches
# set_fee_multiplier is exactly one of FEE_MULTIPLIERS and not a float Discord
# round-tripped.
MULTIPLIER_CHOICES = [
    app_commands.Choice(name=f"x{multiplier:g}", value=str(multiplier)) for multiplier in FEE_MULTIPLIERS
]
# Strings for the same reason, since 6.25 is one of them.
VAT_CHOICES = [
    app_commands.Choice(name=format_vat(percent), value=str(percent)) for percent in VAT_PERCENTS
]
VAT_KIND_CHOICES = [app_commands.Choice(name=VAT_LABELS[kind], value=kind) for kind in VAT_KINDS]
DENOMINATION_CHOICES = [
    app_commands.Choice(name=format_currency(cents / 100), value=cents)
    for cents in BOND_DENOMINATIONS_CENTS
]

# What one unit of each machine's fee buys, for the status page - the press
# charges per press-day and the blast furnace per batch.
FEE_UNITS = {"press": "press-day", "blast_furnace": f"batch of {BLAST_FURNACE_BATCH_SIZE}"}


def _cents(cents: int, emoji: str | None) -> str:
    return format_currency(cents / 100, emoji)


def _units(cents: int) -> str:
    """A sum of cents as a bare figure, for a line whose heading already
    names the currency."""
    return format_price(cents / 100)


class GovernmentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.government_loop.start()

    def cog_unload(self):
        self.government_loop.cancel()

    vote_group = app_commands.Group(name="vote", description="Vote in this server's Thursday election")
    government_group = app_commands.Group(name="government", description="This server's Mayor, Treasurer and treasury")
    treasurer_group = app_commands.Group(name="treasurer", description="The Treasurer's settings (Treasurer only)")
    mayor_group = app_commands.Group(name="mayor", description="The Mayor's projects (Mayor only)")
    bonds_group = app_commands.Group(name="bonds", description="Lend this server currency and be repaid out of its VAT")

    # -----------------------------------------------------------------------
    # Membership, and the lazy count
    # -----------------------------------------------------------------------

    def _membership_check(self, guild: discord.Guild | None):
        """An is_member callable for count_election, or None when the guild
        isn't reachable. The member cache is off (bot.py), so this asks the
        gateway. An error other than "not a member" counts as present: a
        Discord hiccup must not cost somebody an office they won."""
        if guild is None:
            return None

        async def is_member(user_id: int) -> bool:
            try:
                await guild.fetch_member(user_id)
                return True
            except discord.NotFound:
                return False
            except discord.HTTPException:
                return True

        return is_member

    async def _count_if_due(self, guild: discord.Guild | None, guild_id: int):
        try:
            await count_election(self.db, guild_id, is_member=self._membership_check(guild))
        except Exception:
            # A failed count is retried by the next command or the next loop
            # tick; it is not worth failing the command that happened to
            # trigger it.
            log.exception("Counting the election in guild %s failed.", guild_id)

    async def _refuse(self, interaction: discord.Interaction, message: str):
        await interaction.response.send_message(message, ephemeral=True)

    async def _currency_emoji(self, guild_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT currency_emoji FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["currency_emoji"] if row else None

    # -----------------------------------------------------------------------
    # /vote
    # -----------------------------------------------------------------------

    async def _vote(self, interaction: discord.Interaction, office: str, member: discord.Member):
        await ensure_server_row(self.db, interaction.guild_id)
        await self._count_if_due(interaction.guild, interaction.guild_id)
        try:
            async with self.db.transaction() as tx:
                await cast_vote(
                    tx, interaction.guild_id, office, interaction.user.id, member.id,
                    candidate_is_bot=member.bot,
                )
        except GovernmentError as exc:
            await self._refuse(interaction, str(exc))
            return
        closes = int(game_midnight(next_voting_day()).timestamp()) + 24 * 3600
        embed = build_action_receipt(
            "🗳️ Vote Cast", GOVERNMENT_COLOR,
            f"You voted for {member.mention} as **{OFFICE_LABELS[office]}**. "
            f"Voting closes <t:{closes}:R>.",
            [],
            footer_note="only your latest vote counts",
        )
        await respond(interaction, self.db, embed=embed)

    @vote_group.command(name="mayor", description="Vote for this server's Mayor (Thursdays only)")
    @app_commands.describe(member="Who should be Mayor - anyone but you")
    async def vote_mayor(self, interaction: discord.Interaction, member: discord.Member):
        await self._vote(interaction, MAYOR, member)

    @vote_group.command(name="treasurer", description="Vote for this server's Treasurer (Thursdays only)")
    @app_commands.describe(member="Who should be Treasurer - anyone but you")
    async def vote_treasurer(self, interaction: discord.Interaction, member: discord.Member):
        await self._vote(interaction, TREASURER, member)

    # -----------------------------------------------------------------------
    # /government status
    # -----------------------------------------------------------------------

    @government_group.command(name="status", description="Who holds office, the VAT, the treasury and the projects")
    async def government_status_command(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        await self._count_if_due(interaction.guild, interaction.guild_id)
        status = await government_status(self.db, interaction.guild_id)
        quote = await bonanza_quote(self.db, interaction.guild_id)
        emoji = status.currency_emoji or DEFAULT_CURRENCY_EMOJI

        # Laid out the way a machine status page is (docs/stylization.md): the
        # header says whose government, the title carries the Treasurer's two
        # headline settings, the description says who holds office, and the
        # fields are figures. Until 1.4.1 the title repeated the header and the
        # fees and projects were two five-line lists naming every machine twice.
        embed = make_embed(
            f"Fee VAT {format_vat(status.fee_vat_percent)} · Market VAT {format_vat(status.market_vat_percent)} · "
            f"Bond rate {status.bond_rate_percent}%",
            GOVERNMENT_COLOR,
        )
        if interaction.guild is not None:
            embed.set_author(name=f"🏛️ Government • {interaction.guild.name}")

        def holder(user_id):
            return f"<@{user_id}>" if user_id else "*vacant*"

        if voting_open():
            closes = int(game_midnight(next_voting_day()).timestamp()) + 24 * 3600
            election = f"🗳️ **Voting is open** until <t:{closes}:t> · `/vote mayor`, `/vote treasurer`"
        else:
            opens = int(game_midnight(next_voting_day()).timestamp())
            election = f"Next vote opens <t:{opens}:R>"
        embed.description = (
            f"**Mayor** {holder(status.mayor)} · **Treasurer** {holder(status.treasurer)}\n"
            f"{election}"
        )

        # Money, named once per heading rather than on every figure.
        embed.add_field(
            name=f"Treasury · {emoji}", value=f"**{format_price(status.treasury)}**", inline=True,
        )
        embed.add_field(
            name=f"Owed to bondholders · {emoji}",
            value=f"`{_units(status.debt_cents)}` of `{_units(status.cap_cents)}` cap",
            inline=True,
        )
        if status.sale_cents:
            embed.add_field(
                name=f"Bonds for sale · {emoji}",
                value=f"**{_units(status.sale_cents)}** at {status.bond_rate_percent}% · `/bonds buy`",
                inline=True,
            )
        if status.repayment_pool > 0:
            embed.add_field(
                name=f"Repaying this hour · {emoji}",
                value=f"`{format_price(status.repayment_pool)}`",
                inline=True,
            )
        if status.frozen_debt_cents:
            embed.add_field(
                name=f"Frozen · {emoji}",
                value=f"`{_units(status.frozen_debt_cents)}` owed to members who left",
                inline=True,
            )

        # One line per machine: its fee and multiplier, then its enhancement
        # and what the next one costs.
        machine_lines = []
        for m in MACHINES:
            level = status.enhancements[m]
            fee = format_exact_price(machine_fee(m, status.multipliers[m]))
            machine_lines.append(
                f"{machine_display(m)} `{fee}`/{FEE_UNITS.get(m, 'item')} "
                f"x{status.multipliers[m]:g} · enh. {level} (x{enhancement_speed(level):g}) · "
                f"next `{format_price(enhancement_price(level))}`"
            )
        embed.add_field(
            name=f"Machines · fee · enhancement · {emoji}",
            value="\n".join(machine_lines),
            inline=False,
        )

        if quote.running_until:
            bonanza = f"🎉 **Running** · double speed until {format_relative_timestamp(hours_until(quote.running_until))}"
        elif quote.available_from is not None:
            bonanza = "Available once this server has a week of production"
        else:
            bonanza = f"{format_currency(quote.price, emoji)} for {BONANZA_HOURS} hours of double speed"
        embed.add_field(name="Bonanza", value=bonanza, inline=False)

        await respond(interaction, self.db, embed=embed)

    # -----------------------------------------------------------------------
    # /treasurer
    # -----------------------------------------------------------------------

    async def _treasurer_action(self, interaction: discord.Interaction, action, title: str, summary: str):
        await ensure_server_row(self.db, interaction.guild_id)
        await self._count_if_due(interaction.guild, interaction.guild_id)
        try:
            async with self.db.transaction() as tx:
                await action(tx)
        except GovernmentError as exc:
            await self._refuse(interaction, str(exc))
            return
        embed = build_action_receipt(title, GOVERNMENT_COLOR, summary, [])
        await respond(interaction, self.db, embed=embed)

    @treasurer_group.command(name="fee", description="Set a machine's fee as a multiple of its default (once a day)")
    @app_commands.describe(machine="Which machine", multiplier="Its fee as a multiple of the default")
    @app_commands.choices(machine=MACHINE_CHOICES, multiplier=MULTIPLIER_CHOICES)
    async def treasurer_fee(
        self, interaction: discord.Interaction,
        machine: app_commands.Choice[str], multiplier: app_commands.Choice[str],
    ):
        value = float(multiplier.value)
        emoji = await self._currency_emoji(interaction.guild_id)
        await self._treasurer_action(
            interaction,
            lambda tx: set_fee_multiplier(tx, interaction.guild_id, interaction.user.id, machine.value, value),
            "🏛️ Fee Set",
            f"{machine_display(machine.value)} now charges "
            f"**{format_exact_currency(machine_fee(machine.value, value), emoji)}** per "
            f"{FEE_UNITS.get(machine.value, 'item')} (x{value:g}).",
        )

    @treasurer_group.command(name="vat", description="Set the Fee VAT or the Market VAT (each once a day)")
    @app_commands.describe(
        kind="Fee: a share of every machine fee. Market: a share of every trade between players",
        percent="Taken out of the fee or the seller's proceeds, never added on top",
    )
    @app_commands.choices(kind=VAT_KIND_CHOICES, percent=VAT_CHOICES)
    async def treasurer_vat(
        self, interaction: discord.Interaction,
        kind: app_commands.Choice[str], percent: app_commands.Choice[str],
    ):
        value = float(percent.value)
        what = "every machine fee" if kind.value == FEE_VAT else "every trade between players"
        await self._treasurer_action(
            interaction,
            lambda tx: set_vat(tx, interaction.guild_id, interaction.user.id, kind.value, value),
            f"🏛️ {VAT_LABELS[kind.value]} Set",
            f"The {VAT_LABELS[kind.value]} is now **{format_vat(value)}** of {what}.",
        )

    @treasurer_group.command(name="bondrate", description="Set the premium new bonds repay (once a day)")
    @app_commands.describe(percent=f"0-{MAX_BOND_RATE_PERCENT}%, paid once on top of what a bond cost")
    async def treasurer_bondrate(
        self, interaction: discord.Interaction,
        percent: app_commands.Range[int, 0, MAX_BOND_RATE_PERCENT],
    ):
        await self._treasurer_action(
            interaction,
            lambda tx: set_bond_rate(tx, interaction.guild_id, interaction.user.id, percent),
            "🏛️ Bond Rate Set",
            f"New bonds repay **{percent}%** on top of what they cost.",
        )

    # -----------------------------------------------------------------------
    # /mayor
    # -----------------------------------------------------------------------

    async def _mayor_action(self, interaction: discord.Interaction, action):
        """Runs `action(tx)` as the Mayor. It returns (title, summary, spent,
        fields): what the receipt says, what it took from the treasury (None
        for a project that spends nothing), and any further fields. The spend
        is shown with what the treasury holds afterwards, read in the same
        transaction."""
        await ensure_server_row(self.db, interaction.guild_id)
        await self._count_if_due(interaction.guild, interaction.guild_id)
        emoji = await self._currency_emoji(interaction.guild_id)
        try:
            async with self.db.transaction() as tx:
                title, summary, spent, fields = await action(tx)
                row = await tx.fetchone(
                    "SELECT treasury FROM server_config WHERE guild_id = ?", (interaction.guild_id,)
                )
        except GovernmentError as exc:
            await self._refuse(interaction, str(exc))
            return
        if spent is not None:
            fields = [(
                "Spent",
                currency_line(spent, row["treasury"], emoji, gained=False, after_label="left in the treasury"),
            )] + fields
        embed = build_action_receipt(title, GOVERNMENT_COLOR, summary, fields)
        await respond(interaction, self.db, embed=embed)

    @mayor_group.command(name="fund", description="Pay treasury money into a machine's upgrade fund")
    @app_commands.describe(machine="Which machine", amount="How much of the treasury to spend")
    @app_commands.choices(machine=MACHINE_CHOICES)
    async def mayor_fund(
        self, interaction: discord.Interaction,
        machine: app_commands.Choice[str], amount: app_commands.Range[float, 0.01],
    ):
        async def action(tx):
            level = await fund_machine(tx, interaction.guild_id, interaction.user.id, machine.value, amount)
            return (
                "🏛️ Machine Funded",
                f"Funded the {machine_display(machine.value)}, now at level **{level:,}**.",
                amount, [],
            )

        await self._mayor_action(interaction, action)

    @mayor_group.command(name="enhance", description="Buy a machine an Infrastructure Enhancement - double its speed")
    @app_commands.describe(machine="Which machine")
    @app_commands.choices(machine=MACHINE_CHOICES)
    async def mayor_enhance(self, interaction: discord.Interaction, machine: app_commands.Choice[str]):
        emoji = await self._currency_emoji(interaction.guild_id)

        async def action(tx):
            level, price = await buy_enhancement(tx, interaction.guild_id, interaction.user.id, machine.value)
            return (
                "🏛️ Enhancement Bought",
                f"{machine_display(machine.value)} is at enhancement **{level:,}**: "
                f"**x{enhancement_speed(level):g}** the speed its own level gives it.",
                price,
                [("Next enhancement", format_currency(enhancement_price(level), emoji))],
            )

        await self._mayor_action(interaction, action)

    @mayor_group.command(name="slots", description=f"Mining Slot Enhancement - every 1 spent counts {MINING_SLOT_ENHANCEMENT_MULTIPLIER} toward mining slots")
    @app_commands.describe(amount="How much of the treasury to spend")
    async def mayor_slots(self, interaction: discord.Interaction, amount: app_commands.Range[float, 0.01]):
        emoji = await self._currency_emoji(interaction.guild_id)

        async def action(tx):
            credit = await fund_mining_slots(tx, interaction.guild_id, interaction.user.id, amount)
            slots = await mining_slot_status(tx, interaction.guild_id)
            return (
                "🏛️ Mining Slots Funded",
                f"Bought **{format_currency(credit, emoji)}** of mining slot progress.",
                amount,
                [(
                    "Mining slots",
                    f"{format_currency(slots.progress, emoji)} / "
                    f"{format_currency(slots.next_threshold, emoji)} to the next · "
                    f"**{slots.slots:,}** per player",
                )],
            )

        await self._mayor_action(interaction, action)

    @mayor_group.command(name="bonanza", description="Start a Server Bonanza - 48 hours of double-speed drills and machines")
    async def mayor_bonanza(self, interaction: discord.Interaction):
        async def action(tx):
            quote = await start_bonanza(tx, interaction.guild_id, interaction.user.id)
            return (
                "🎉 Bonanza Started",
                "Every drill and machine here runs at double speed until "
                f"{format_relative_timestamp(BONANZA_HOURS)}.",
                quote.price, [],
            )

        await self._mayor_action(interaction, action)

    @mayor_group.command(name="bonds", description="Put bonds up for sale, replacing any sale open (0 withdraws it)")
    @app_commands.describe(amount="The total to raise, in whole currency units")
    async def mayor_bonds(self, interaction: discord.Interaction, amount: app_commands.Range[int, 0]):
        emoji = await self._currency_emoji(interaction.guild_id)

        async def action(tx):
            await open_bond_sale(tx, interaction.guild_id, interaction.user.id, amount * 100)
            if not amount:
                return "🏛️ Bond Sale Withdrawn", "No bonds are for sale now.", None, []
            status = await government_status(tx, interaction.guild_id)
            return (
                "🏛️ Bonds Offered",
                f"**{format_currency(amount, emoji)}** of bonds are for sale at a "
                f"**{status.bond_rate_percent}%** premium. Players buy them with `/bonds buy`.",
                None, [],
            )

        await self._mayor_action(interaction, action)

    # -----------------------------------------------------------------------
    # /bonds
    # -----------------------------------------------------------------------

    @bonds_group.command(name="buy", description="Buy a bond from the Mayor's sale")
    @app_commands.describe(denomination="How much to lend")
    @app_commands.choices(denomination=DENOMINATION_CHOICES)
    async def bonds_buy(self, interaction: discord.Interaction, denomination: app_commands.Choice[int]):
        await ensure_server_row(self.db, interaction.guild_id)
        await self._count_if_due(interaction.guild, interaction.guild_id)
        emoji = await self._currency_emoji(interaction.guild_id)
        try:
            async with self.db.transaction() as tx:
                bond = await buy_bond(tx, interaction.guild_id, interaction.user.id, denomination.value)
                balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
        except (GovernmentError, InsufficientQuantity) as exc:
            await self._refuse(interaction, str(exc))
            return
        embed = build_action_receipt(
            "🏛️ Bond Bought", GOVERNMENT_COLOR,
            f"You lent the server **{_cents(bond.principal_cents, emoji)}**.",
            [
                ("Lent", currency_line(bond.principal_cents / 100, balance, emoji, gained=False)),
                ("Repays", f"{_cents(bond.owed_cents, emoji)} · {bond.rate_percent}% on top, hourly from VAT"),
            ],
            footer_note="/bonds holdings shows what is still owed",
        )
        await respond(interaction, self.db, embed=embed)

    @bonds_group.command(name="holdings", description="The bonds this server still owes you")
    async def bonds_holdings(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        emoji = await self._currency_emoji(interaction.guild_id) or DEFAULT_CURRENCY_EMOJI
        bonds = await bonds_held(self.db, interaction.guild_id, interaction.user.id)
        embed = make_embed("🏛️ Your Bonds", GOVERNMENT_COLOR)
        if not bonds:
            embed.description = "This server owes you nothing."
        else:
            # The id leads in code format, as /market entries' rows do, and the
            # currency is named once in the heading.
            lines = [
                f"`#{bond['bond_id']}` lent `{_units(bond['principal_cents'])}` · "
                f"`{_units(bond['remaining_cents'])}` of `{_units(bond['owed_cents'])}` still to come"
                + (" · frozen" if bond["frozen"] else "")
                for bond in bonds
            ]
            add_multi_field(embed, f"Bonds · {emoji}", lines)
        await respond(interaction, self.db, embed=embed)

    # -----------------------------------------------------------------------
    # Members leaving and returning
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        # Raw rather than on_member_remove: the member cache is off (bot.py),
        # and the raw event fires whether or not the member was cached.
        async with self.db.transaction() as tx:
            await member_left(tx, payload.guild_id, payload.user.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        async with self.db.transaction() as tx:
            await member_returned(tx, member.guild.id, member.id)

    # -----------------------------------------------------------------------
    # The hourly loop
    # -----------------------------------------------------------------------

    @tasks.loop(hours=1)
    async def government_loop(self):
        """One pass of the hourly work. Every step is per server and guarded on
        its own: an unhandled exception would stop a tasks.loop for good, and
        one server's bad row must not stop every other server's bondholders
        being paid."""
        if voting_open():
            rows = await self.db.fetchall(
                "SELECT guild_id FROM server_config WHERE bot_present = 1 "
                "AND (election_announced IS NULL OR election_announced != ?)",
                (game_date(),),
            )
            for row in rows:
                await self._guarded("announcing the vote", row["guild_id"], self._announce, row["guild_id"])

        for guild_id in await guilds_with_due_votes(self.db):
            await self._count_if_due(self.bot.get_guild(guild_id), guild_id)

        for guild_id in await guilds_with_repayments(self.db):
            await self._guarded("paying bondholders", guild_id, self._pay, guild_id)

        # A creditor who rejoined while the bot was offline never produced an
        # on_member_join; ask Discord. Frozen bonds are rare, so this is a
        # handful of requests at most.
        for guild_id, holder_id in await frozen_holders(self.db):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            try:
                await guild.fetch_member(holder_id)
            except discord.HTTPException:
                continue
            await self._guarded("unfreezing bonds", guild_id, self._unfreeze, guild_id, holder_id)

        await prune_tax_history(self.db)

    async def _guarded(self, what: str, guild_id: int, step, *args):
        try:
            await step(*args)
        except Exception:
            log.exception("Government loop: %s in guild %s failed.", what, guild_id)

    async def _announce(self, guild_id: int):
        async with self.db.transaction() as tx:
            await announce_voting(tx, guild_id)

    async def _pay(self, guild_id: int):
        async with self.db.transaction() as tx:
            await pay_bondholders(tx, guild_id)

    async def _unfreeze(self, guild_id: int, holder_id: int):
        async with self.db.transaction() as tx:
            await member_returned(tx, guild_id, holder_id)

    @government_loop.before_loop
    async def before_government_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(GovernmentCog(bot))
