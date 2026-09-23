"""
cogs/government.py

Implements the server government (1.4):
  - /vote mayor <member>, /vote treasurer <member>  - Thursdays only
  - /government status                             - who holds office, the
                                                     settings, the treasury,
                                                     the debt and the projects
  - /treasurer fee <machine> <multiplier>          - Treasurer only
  - /treasurer tax <percent>
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
    MINING_SLOT_ENHANCEMENT_MULTIPLIER,
    enhancement_price,
    enhancement_speed,
)
from utils.db_helpers import (
    MACHINES,
    ensure_server_row,
    machine_fee,
    machine_label,
    mining_slot_status,
)
from utils.embeds import GOVERNMENT_COLOR, make_embed
from utils.formatting import format_currency
from utils.government import (
    BOND_DENOMINATIONS_CENTS,
    DEBT_CAP_DAYS,
    FEE_MULTIPLIERS,
    MAX_BOND_RATE_PERCENT,
    MAX_TAX_PERCENT,
    MAYOR,
    OFFICE_LABELS,
    TREASURER,
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
    set_tax,
    start_bonanza,
    voting_open,
)
from utils.responses import respond

log = logging.getLogger("dragonhoard")

MACHINE_CHOICES = [
    app_commands.Choice(name=machine_label(machine), value=machine) for machine in MACHINES
]
# Strings rather than floats on the wire, so the value that reaches
# set_fee_multiplier is exactly one of FEE_MULTIPLIERS and not a float Discord
# round-tripped.
MULTIPLIER_CHOICES = [
    app_commands.Choice(name=f"x{multiplier:g}", value=str(multiplier)) for multiplier in FEE_MULTIPLIERS
]
DENOMINATION_CHOICES = [
    app_commands.Choice(name=format_currency(cents / 100), value=cents)
    for cents in BOND_DENOMINATIONS_CENTS
]

# What one unit of each machine's fee buys, for the status page - the press
# charges per press-day and the blast furnace per batch.
FEE_UNITS = {"press": "press-day", "blast_furnace": f"batch of {BLAST_FURNACE_BATCH_SIZE}"}


def _cents(cents: int, emoji: str | None) -> str:
    return format_currency(cents / 100, emoji)


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
    bonds_group = app_commands.Group(name="bonds", description="Lend this server currency and be repaid out of its tax")

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
        embed = make_embed("🗳️ Vote Cast", GOVERNMENT_COLOR)
        embed.description = (
            f"You voted for {member.mention} as **{OFFICE_LABELS[office]}**. Voting closes "
            f"<t:{closes}:R>; you can change your vote until then, and only your latest counts."
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

    @government_group.command(name="status", description="Who holds office, the tax, the treasury and the projects")
    async def government_status_command(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        await self._count_if_due(interaction.guild, interaction.guild_id)
        status = await government_status(self.db, interaction.guild_id)
        quote = await bonanza_quote(self.db, interaction.guild_id)
        emoji = status.currency_emoji

        embed = make_embed("🏛️ Government", GOVERNMENT_COLOR)
        if interaction.guild is not None:
            embed.set_author(name=f"🏛️ Government • {interaction.guild.name}")

        def holder(user_id):
            return f"<@{user_id}>" if user_id else "*vacant*"

        if voting_open():
            closes = int(game_midnight(next_voting_day()).timestamp()) + 24 * 3600
            election = f"🗳️ **Voting is open** until <t:{closes}:t> - `/vote mayor`, `/vote treasurer`"
        else:
            opens = int(game_midnight(next_voting_day()).timestamp())
            election = f"Next vote opens <t:{opens}:R>"
        embed.description = (
            f"**Mayor:** {holder(status.mayor)}\n"
            f"**Treasurer:** {holder(status.treasurer)}\n{election}"
        )

        fee_lines = [
            f"{machine_label(m).capitalize()}: {format_currency(machine_fee(m, status.multipliers[m]), emoji)} "
            f"per {FEE_UNITS.get(m, 'item')} (x{status.multipliers[m]:g})"
            for m in MACHINES
        ]
        embed.add_field(
            name=f"Treasurer's Settings • tax {status.tax_percent}% • bond rate {status.bond_rate_percent}%",
            value="\n".join(fee_lines),
            inline=False,
        )

        treasury_lines = [
            f"Treasury: **{format_currency(status.treasury, emoji)}**",
            f"Owed to bondholders: {_cents(status.debt_cents, emoji)} of a "
            f"{_cents(status.cap_cents, emoji)} cap ({DEBT_CAP_DAYS} days' tax)",
        ]
        if status.repayment_pool > 0:
            treasury_lines.append(
                f"Waiting to be repaid: {format_currency(status.repayment_pool, emoji)} (paid hourly)"
            )
        if status.frozen_debt_cents:
            treasury_lines.append(
                f"Frozen (owed to members who left): {_cents(status.frozen_debt_cents, emoji)}"
            )
        if status.sale_cents:
            treasury_lines.append(
                f"**Bonds for sale:** {_cents(status.sale_cents, emoji)} at "
                f"{status.bond_rate_percent}% - `/bonds buy`"
            )
        embed.add_field(name="Treasury", value="\n".join(treasury_lines), inline=False)

        project_lines = []
        for m in MACHINES:
            level = status.enhancements[m]
            project_lines.append(
                f"{machine_label(m).capitalize()}: enhancement {level} "
                f"(x{enhancement_speed(level):g}) - next {format_currency(enhancement_price(level), emoji)}"
            )
        if quote.running_until:
            project_lines.append(f"🎉 **Bonanza running** - everything at double speed")
        elif quote.available_from is not None:
            project_lines.append("Bonanza: available once this server has a week of production")
        else:
            project_lines.append(f"Bonanza: {format_currency(quote.price, emoji)} for 48 hours at double speed")
        embed.add_field(name="Projects", value="\n".join(project_lines), inline=False)

        await respond(interaction, self.db, embed=embed)

    # -----------------------------------------------------------------------
    # /treasurer
    # -----------------------------------------------------------------------

    async def _treasurer_action(self, interaction: discord.Interaction, action, confirmation: str):
        await ensure_server_row(self.db, interaction.guild_id)
        await self._count_if_due(interaction.guild, interaction.guild_id)
        try:
            async with self.db.transaction() as tx:
                await action(tx)
        except GovernmentError as exc:
            await self._refuse(interaction, str(exc))
            return
        embed = make_embed("🏛️ Treasurer", GOVERNMENT_COLOR, description=confirmation)
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
            f"The {machine_label(machine.value)} now charges "
            f"**{format_currency(machine_fee(machine.value, value), emoji)}** per "
            f"{FEE_UNITS.get(machine.value, 'item')} (x{value:g} its default).",
        )

    @treasurer_group.command(name="tax", description="Set the share of every machine fee the government keeps (once a day)")
    @app_commands.describe(percent=f"0-{MAX_TAX_PERCENT}%")
    async def treasurer_tax(
        self, interaction: discord.Interaction,
        percent: app_commands.Range[int, 0, MAX_TAX_PERCENT],
    ):
        await self._treasurer_action(
            interaction,
            lambda tx: set_tax(tx, interaction.guild_id, interaction.user.id, percent),
            f"The tax is now **{percent}%** of every machine fee. That share goes to the "
            f"treasury - or to bondholders while the server owes any - instead of being burned.",
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
            f"Bonds sold from now on repay **{percent}%** on top of what they cost. Bonds "
            f"already sold keep the rate they were sold at.",
        )

    # -----------------------------------------------------------------------
    # /mayor
    # -----------------------------------------------------------------------

    async def _mayor_action(self, interaction: discord.Interaction, action):
        """Runs `action(tx)`, which returns the confirmation text, as the Mayor."""
        await ensure_server_row(self.db, interaction.guild_id)
        await self._count_if_due(interaction.guild, interaction.guild_id)
        try:
            async with self.db.transaction() as tx:
                confirmation = await action(tx)
        except GovernmentError as exc:
            await self._refuse(interaction, str(exc))
            return
        embed = make_embed("🏛️ Mayor", GOVERNMENT_COLOR, description=confirmation)
        await respond(interaction, self.db, embed=embed)

    @mayor_group.command(name="fund", description="Pay treasury money into a machine's upgrade fund")
    @app_commands.describe(machine="Which machine", amount="How much of the treasury to spend")
    @app_commands.choices(machine=MACHINE_CHOICES)
    async def mayor_fund(
        self, interaction: discord.Interaction,
        machine: app_commands.Choice[str], amount: app_commands.Range[float, 0.01],
    ):
        emoji = await self._currency_emoji(interaction.guild_id)

        async def action(tx):
            level = await fund_machine(tx, interaction.guild_id, interaction.user.id, machine.value, amount)
            return (
                f"Put **{format_currency(amount, emoji)}** of the treasury into the "
                f"{machine_label(machine.value)}, which is at level {level:,}. It counts toward "
                f"mining slots too."
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
                f"Spent **{format_currency(price, emoji)}** enhancing the "
                f"{machine_label(machine.value)} to level {level:,}: it now runs at "
                f"**x{enhancement_speed(level):g}** the speed its own level gives it. The next "
                f"enhancement costs {format_currency(enhancement_price(level), emoji)}."
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
                f"Spent **{format_currency(amount, emoji)}** for "
                f"**{format_currency(credit, emoji)}** of mining slot progress. The server is at "
                f"{format_currency(slots.progress, emoji)} of the "
                f"{format_currency(slots.next_threshold, emoji)} its next slot needs, with "
                f"{slots.slots:,} per player now."
            )

        await self._mayor_action(interaction, action)

    @mayor_group.command(name="bonanza", description="Start a Server Bonanza - 48 hours of double-speed drills and machines")
    async def mayor_bonanza(self, interaction: discord.Interaction):
        emoji = await self._currency_emoji(interaction.guild_id)

        async def action(tx):
            quote = await start_bonanza(tx, interaction.guild_id, interaction.user.id)
            return (
                f"Spent **{format_currency(quote.price, emoji)}** on a Server Bonanza. For the next "
                f"48 hours every drill and every machine here runs at double speed."
            )

        await self._mayor_action(interaction, action)

    @mayor_group.command(name="bonds", description="Put bonds up for sale, replacing any sale open (0 withdraws it)")
    @app_commands.describe(amount="The total to raise, in whole currency units")
    async def mayor_bonds(self, interaction: discord.Interaction, amount: app_commands.Range[int, 0]):
        emoji = await self._currency_emoji(interaction.guild_id)

        async def action(tx):
            await open_bond_sale(tx, interaction.guild_id, interaction.user.id, amount * 100)
            if not amount:
                return "Withdrew the bond sale."
            status = await government_status(tx, interaction.guild_id)
            return (
                f"Put **{format_currency(amount, emoji)}** of bonds up for sale at a "
                f"{status.bond_rate_percent}% premium. Players buy them with `/bonds buy`, and "
                f"the server repays them out of tax, hourly."
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
        except (GovernmentError, InsufficientQuantity) as exc:
            await self._refuse(interaction, str(exc))
            return
        embed = make_embed("🏛️ Bond Bought", GOVERNMENT_COLOR)
        embed.description = (
            f"You lent the server **{_cents(bond.principal_cents, emoji)}**. It will repay you "
            f"**{_cents(bond.owed_cents, emoji)}** ({bond.rate_percent}% on top) out of its tax, "
            f"a share every hour, straight to your balance."
        )
        await respond(interaction, self.db, embed=embed)

    @bonds_group.command(name="holdings", description="The bonds this server still owes you")
    async def bonds_holdings(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        emoji = await self._currency_emoji(interaction.guild_id)
        bonds = await bonds_held(self.db, interaction.guild_id, interaction.user.id)
        embed = make_embed("🏛️ Your Bonds", GOVERNMENT_COLOR)
        if not bonds:
            embed.description = "This server owes you nothing."
        else:
            lines = [
                f"#{bond['bond_id']} · lent {_cents(bond['principal_cents'], emoji)} · "
                f"{_cents(bond['remaining_cents'], emoji)} of {_cents(bond['owed_cents'], emoji)} "
                f"still to come" + (" · frozen" if bond["frozen"] else "")
                for bond in bonds
            ]
            embed.description = "\n".join(lines)
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
