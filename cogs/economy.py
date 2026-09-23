"""
cogs/economy.py

Implements:
  - /balance                              - your balance of this server's currency
  - /inventory                            - your inventory alongside your balance
  - /market sell <material> <quantity>    - sell to the server (the currency faucet)
  - /market buy <material> <quantity>     - buy from the server's stock (a currency sink)
  - /market status                        - show the server's current stock and prices
  - /economy status                       - this server's economy at a glance
  - /economy gdp                          - what this server produced, in detail

Per docs/market.md, the server itself is an economic actor with its own
material storage (server_material_storage table). It buys raw/smelted
materials from users at that material's market price and sells them back at
MARKET_BUY_MARKUP times the same figure, constrained by what it actually has
in stock, since the server can't sell what it never acquired.

Both rates are STATIC as of 1.3 (data/materials.py: sale_unit_price,
purchase_unit_price). They used to decay with the server's stock - full price
at zero stock, half at the server's "target stock", tapering toward zero
beyond it - which meant a price was only knowable by running the command, a
large sale paid a rate the player could not have worked out in advance, and
every figure quoted anywhere had to say which stock level it was quoted at.
The stock itself still matters: it is what the server can sell back, what the
furnace's auto-smelt reads, and what the job board picks a material from.

/economy lives here rather than in a cog of its own because it is not a new
feature area: every figure it reports is the market's own faucet-and-sink
accounting (docs/market.md section 4) read from a different angle, and it
shares MARKET_COLOR with /market for that reason - the way /balance shares
INVENTORY_COLOR with /inventory. See docs/stylization.md.

It is a group of two pages rather than one command. /economy status answers
"where does this server stand" in a screenful; /economy gdp answers "what did
it produce, and where did that come from", which is three fields of accounting
that a player checking a queue length did not ask for. Discord will not let a
command with subcommands be invoked on its own, which is why the overview is
named at all.

DragonCoin (users.dragoncoin) is intentionally NOT surfaced here or anywhere
else - per docs/market.md section 2, it exists solely as a future conceptual
unit for cross-server exchange rates and isn't spendable, earnable, or shown
in any menu.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.responses import respond
from utils.embeds import (
    make_embed,
    add_multi_field,
    queue_field_name,
    INVENTORY_COLOR,
    MARKET_COLOR,
)
from utils.formatting import (
    format_currency,
    format_duration,
    format_compact_price,
    format_price,
    format_relative_timestamp,
    plural,
    DEFAULT_CURRENCY_EMOJI,
)
from utils.receipts import build_market_receipt_embed, fill_lines
from utils.guild_helpers import human_member_count
from utils.market_book import (
    SERVER,
    listed_drills,
    listing_depth,
    order_depth,
    own_entries,
    bid_quantities,
    listed_quantities,
    server_price_units,
    consume_listing,
    consume_order,
    fills_total,
    listing_price_error,
    order_price_error,
    permanent_material_error,
    plan_buy,
    plan_sell,
    server_quantity,
)
from utils.job_board import credit_job_progress, ensure_todays_job, hours_until_reset
from utils.drills import (
    DRILL_AVAILABLE_SQL,
    DrillScope,
    drill_cell,
    drill_choices,
    container_name,
    drill_emoji,
    drill_label,
    drill_short_label,
    drill_unavailable_message,
    fetch_drill,
)
from utils.production_ledger import (
    GDP_DAY_HOURS,
    GDP_SOURCES,
    GDP_WEEK_HOURS,
    gem_counts,
    tracked_since,
    window_cutoff,
    window_totals,
)
from database.db import InsufficientQuantity
from utils.db_helpers import (
    MACHINES,
    circulating_currency_for,
    machine_label,
    run_level,
    slot_progress,
    ensure_user_row,
    ensure_server_row,
    get_user_quantity,
    adjust_user_quantity,
    deduct_user_quantity,
    get_server_stocks,
    adjust_server_stock,
    deduct_server_stock,
    get_currency_balance,
    adjust_currency_balance,
    deduct_currency_balance,
    record_minted,
    record_burned,
)

from data.materials import (
    ALL_MATERIALS,
    material_name,
    DRILLS,
    PERMANENT_MATERIALS,
    PLAYER_PRICE_SCALE,
    BLAST_FURNACE_BATCH_SIZE,
    GEMSTONES,
    PRESS_RECIPES,
    TRADEABLE_ORDER,
    INVENTORY_CATEGORIES,
    blast_furnace_rate,
    factory_rate,
    furnace_rate,
    get_material_info,
    press_rate_per_day,
    player_price_total,
    player_price_units,
    purchase_total,
    purchase_unit_price,
    sale_unit_price,
    scrapper_rate,
)

# Only raw and smelted materials are tradeable through the market - component
# materials and drills are excluded (docs/market.md section 3).
#
# Built in TRADEABLE_ORDER rather than by merging the two tables, because dict
# order IS display order here: this one mapping drives /market status's lines
# and both /market sell's and /market buy's choice lists. That ordering - ores,
# then smelted, then gemstones, each commonest first - lives in
# data/materials.py, derived from drop chances and recipes, so retuning either
# reorders all three surfaces without anyone remembering to.
TRADEABLE_MATERIALS = {material_id: ALL_MATERIALS[material_id] for material_id in TRADEABLE_ORDER}

# How many drills /inventory lists individually before collapsing the rest
# into a count, mirroring the pending-jobs cap in /factory status.
DRILL_DISPLAY_LIMIT = 20

# How many drill cells /inventory fits on one line. Fewer than the six used for
# the plain material grid because a drill cell carries a level and a container
# emoji as well as its own, so it's roughly half again as wide.
DRILL_GRID_COLUMNS = 4


# The most one /market command will move. Raised from 1,000 in 1.3, alongside
# static prices: the old limit was partly a pricing guard - a big enough sale
# used to walk the price down under itself - and partly a display one. Neither
# applies to a flat rate, and a drill wearing a Diamond Container holds 32,000
# items (data/materials.py: effective_capacity), which is thirty-two commands
# to sell at the old limit even if it were all one material.
#
# A million rather than no limit at all because discord.py's Range is what
# produces the "too large" error client-side, before the command is even sent;
# without one the rejection would come from the "you only have N of that"
# branch after a round trip, which reads as a bug rather than as a limit.
MAX_MARKET_QUANTITY = 1_000_000


# The dearest a player may ask or bid per unit. Discord's Range is what
# produces the "too large" error client-side, before the command is even sent,
# the same reason MAX_MARKET_QUANTITY has a ceiling at all. A trillion is far
# past anything the game can produce and still leaves price * quantity exact:
# the largest product is 1e12 * PLAYER_PRICE_SCALE * 1e6 units, which is well
# inside the 2**53 where integers stop being exact.
MAX_MARKET_PRICE = 1_000_000_000_000.0

# The finest price increment, as it appears in the rejection when someone types
# something finer. Derived from the scale rather than written as "0.0001", so
# retuning PLAYER_PRICE_SCALE cannot leave the error message quoting the old one.
SMALLEST_PRICE = f"{1 / PLAYER_PRICE_SCALE:.4f}"

# How an autocomplete value names a drill rather than a material stack. A
# material id is its own value; a drill needs its row id, and the two share one
# command parameter.
DRILL_VALUE_PREFIX = "drill:"

# How /market buy names a specific listing rather than a quantity of some
# material. Distinct from DRILL_VALUE_PREFIX because the two identify
# different things: /market list names a drill the player owns, /market buy
# names an offer somebody else has already made.
LISTING_VALUE_PREFIX = "listing:"

# Matches utils/drills.py's own cap on how many autocomplete results Discord
# will render at once.
MAX_AUTOCOMPLETE_RESULTS = 25

# How many open listings or orders /market status names individually before
# collapsing the rest into a count, mirroring DRILL_DISPLAY_LIMIT on
# /inventory and JOB_DISPLAY_LIMIT on the machine status embeds.
BOOK_DISPLAY_LIMIT = 10

# How many of a player's OWN entries /market entries names before collapsing
# the rest into a count. Higher than BOOK_DISPLAY_LIMIT because that field
# shares an embed with four others on /market status, while this one has a page
# to itself - and because these are the entries the player is there to act on,
# so hiding them behind a count is the thing the page exists to avoid.
ENTRIES_DISPLAY_LIMIT = 40

# Everything a player may place a BID for: every material except the drill
# types, which are not fungible and are sold by listing one specific drill, and
# except Exotic Matter, which is never traded at all (data/materials.py:
# PERMANENT_MATERIALS). Built by exclusion rather than by listing the ids, so a
# new material is orderable by existing and a new exotic one is not by being in
# that table.
#
# Ordered TRADEABLE_ORDER first, since those are the six with a server quote to
# compare against and the ones a player is most likely to be bidding on.
ORDERABLE_MATERIALS: tuple[str, ...] = TRADEABLE_ORDER + tuple(
    material_id for material_id in ALL_MATERIALS
    if material_id not in TRADEABLE_ORDER
    and material_id not in DRILLS
    and material_id not in PERMANENT_MATERIALS
)


# What each machine shows as on /economy's queue lines. The emoji are the ones
# each machine's own status embed already uses (docs/stylization.md), so a
# player recognises the machine here before reading the label. Keyed on
# MACHINES, and tests/test_production_ledger.py pins that it covers all of them
# - a sixth machine appearing with no icon is the failure this catches.
MACHINE_EMOJI = {
    "furnace": "\U0001F525",
    "blast_furnace": "\u2668\ufe0f",
    "factory": "\U0001F3ED",
    "press": "\u2699\ufe0f",
    "scrapper": "\u267B\ufe0f",
}

# Where a machine's throughput comes from. The press is deliberately absent:
# it is the one machine whose queue is measured in press-days rather than
# items, with whatever it has already banked coming off the front, so its wait
# is computed the way /press status computes it rather than as count / rate.
MACHINE_RATE = {
    "furnace": furnace_rate,
    "blast_furnace": blast_furnace_rate,
    "factory": factory_rate,
    "scrapper": scrapper_rate,
}

# How each GDP source reads on the value breakdown. Only the sources GDP
# actually counts appear here - the factory, press and scrapper produce goods
# the market does not price, so they have no value-added figure to show and
# turn up in the import/export sentence instead (utils/production_ledger.py:
# GDP_SOURCES).
GDP_SOURCE_LABEL = {
    "mining": "\u26CF\ufe0f Mined here",
    "furnace": "\U0001F525 Smelted here",
    "blast_furnace": "\u2668\ufe0f Blast-smelted here",
}


def max_affordable(unit_cost: float, balance: float) -> int:
    """How many units a balance covers at a flat per-unit cost.

    Exact arithmetic rather than a search, because the per-unit price does not
    move with quantity (or with anything else) - so the total is simply linear.

    The 1e-9 nudge is the same guard format_price uses: a balance that exactly
    covers N units can land a hair under N on float division, and quoting N-1
    back to someone who can afford N is precisely the confusion this message
    exists to remove.
    """
    if unit_cost <= 0 or balance <= 0:
        return 0
    return int(balance / unit_cost + 1e-9)


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def _get_currency_emoji(self, guild_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT currency_emoji FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["currency_emoji"] if row else None

    def _cannot_afford_message(
        self,
        material_id: str,
        quantity: int,
        total_cost: float,
        balance: float,
        currency_emoji: str | None,
    ) -> str:
        """The /market buy rejection, which also names the largest quantity the
        user COULD buy right now. Being told only "you can't afford this" leaves
        them bisecting by hand, which is a pointless thing to make someone do
        when the answer is one division away."""
        unit_cost = purchase_unit_price(material_id)
        affordable = max_affordable(unit_cost, balance)
        # The requested quantity is by definition unaffordable - we're in this
        # branch because of it - so float noise must never let it be quoted back
        # as the answer.
        affordable = min(affordable, quantity - 1)

        shortfall = (
            f"This costs {format_currency(total_cost, currency_emoji, True)}, "
            f"but you only have {format_currency(balance, currency_emoji)}."
        )
        if affordable < 1:
            return f"{shortfall} That isn't enough for even one."
        return (
            f"{shortfall} You can afford up to **{affordable:,}**, "
            f"for {format_currency(purchase_total(material_id, affordable), currency_emoji, True)}."
        )

    async def _currency_lines(self, interaction: discord.Interaction) -> list[str]:
        """Every server currency balance this user holds, formatted for
        display and labelled with the currency's own name. The current
        server's currency always comes first (even if the balance is 0),
        followed by every other server's currency ordered highest balance to
        lowest.

        Servers Dragonhoard has been removed from are filtered out by
        bot_present: their balances are deliberately kept in the database (a
        re-invite restores them untouched), but a currency you have no way to
        earn or spend is just noise in a list you read to decide what to do
        next. The current server needs no such guard - running a command in it
        proves the bot is there."""
        rows = await self.db.fetchall(
            """
            SELECT scb.guild_id, scb.balance, sc.currency_name, sc.currency_emoji
            FROM server_currency_balances scb
            JOIN server_config sc ON sc.guild_id = scb.guild_id
            WHERE scb.user_id = ? AND sc.bot_present = 1
            """,
            (interaction.user.id,),
        )
        by_guild = {row["guild_id"]: row for row in rows}

        ordered_rows = []
        if interaction.guild_id is not None:
            current = by_guild.pop(interaction.guild_id, None)
            if current is None:
                server_cfg = await self.db.fetchone(
                    "SELECT currency_name, currency_emoji FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                current = {
                    "guild_id": interaction.guild_id,
                    "balance": 0.0,
                    "currency_name": server_cfg["currency_name"] if server_cfg else None,
                    "currency_emoji": server_cfg["currency_emoji"] if server_cfg else None,
                }
            ordered_rows.append(current)

        ordered_rows.extend(sorted(by_guild.values(), key=lambda r: r["balance"], reverse=True))

        lines = []
        for row in ordered_rows:
            emoji = row["currency_emoji"] if row["currency_name"] else None
            # A server that hasn't run /setup currency has no currency name
            # yet, so fall back to naming the server itself.
            guild = self.bot.get_guild(row["guild_id"])
            label = row["currency_name"] or (guild.name if guild else f"Server {row['guild_id']}")
            lines.append(f"{format_currency(row['balance'], emoji)} {label}")
        return lines

    @app_commands.command(name="balance", description="Check your currency balances across every server")
    async def balance(self, interaction: discord.Interaction):
        embed = make_embed(f"{interaction.user.display_name}'s Balance", INVENTORY_COLOR)
        lines = await self._currency_lines(interaction)
        add_multi_field(embed, "Currencies", lines)
        await respond(interaction, self.db, embed=embed)

    @app_commands.command(name="inventory", description="Show your inventory alongside your balance")
    async def inventory(self, interaction: discord.Interaction):
        embed = make_embed(f"{interaction.user.display_name}'s Inventory", INVENTORY_COLOR)

        # _currency_lines puts this server first, then every other server by
        # balance descending - so the first three are this server plus the
        # user's two largest balances elsewhere.
        currency_lines = await self._currency_lines(interaction)
        embed.description = "\n".join(currency_lines[:3])

        inventory_rows = await self.db.fetchall(
            "SELECT material_id, quantity FROM user_materials WHERE user_id = ? AND quantity > 0",
            (interaction.user.id,),
        )
        quantities = {row["material_id"]: row["quantity"] for row in inventory_rows}

        has_items = False
        for field_name, material_ids in INVENTORY_CATEGORIES:
            cells = []
            for material_id in material_ids:
                quantity = quantities.get(material_id)
                info = get_material_info(material_id)
                if not quantity or info is None:
                    continue
                cells.append(f"{info['emoji']} {quantity:,}")
            if not cells:
                continue
            has_items = True
            grid_lines = [" ".join(cells[i:i + 6]) for i in range(0, len(cells), 6)]
            add_multi_field(embed, field_name, grid_lines)

        # Drills get their own field rather than sharing the grid above: they
        # aren't stacks in user_materials any more but individual rows, each
        # with its own level and container, so a bare "{emoji} {count}" cell
        # can't say what any one of them actually is.
        #
        # Only UNPLACED, UNLOCKED drills are listed. A placed drill isn't in
        # your inventory in any sense that matters - you can't craft with it,
        # fit a container to it or place it somewhere else - so listing it
        # here made the field read as a roster rather than as stock on hand.
        # /mine status is where a server's placed drills are shown. A drill
        # that is unavailable for any other reason is excluded on the same
        # grounds - one queued for an upgrade or handed to the scrapper, and
        # since 1.4 one listed on the player market. DRILL_AVAILABLE_SQL is
        # that rule (utils/drills.py: drill_unavailable_reason); spelling the
        # columns out here is what let the last one be forgotten.
        drill_rows = await self.db.fetchall(
            "SELECT * FROM drills WHERE owner_id = ? AND guild_id IS NULL "
            f"AND {DRILL_AVAILABLE_SQL} "
            "ORDER BY level DESC, drill_id ASC",
            (interaction.user.id,),
        )
        if drill_rows:
            has_items = True
            cells = [drill_cell(row) for row in drill_rows[:DRILL_DISPLAY_LIMIT]]
            grid_lines = [
                " ".join(cells[i:i + DRILL_GRID_COLUMNS])
                for i in range(0, len(cells), DRILL_GRID_COLUMNS)
            ]
            if len(drill_rows) > DRILL_DISPLAY_LIMIT:
                grid_lines.append(f"... and {len(drill_rows) - DRILL_DISPLAY_LIMIT} more")
            add_multi_field(embed, "Drills", grid_lines)

        if not has_items:
            embed.add_field(name="Items", value="Your inventory is empty.", inline=False)

        await respond(interaction, self.db, embed=embed)

    market_group = app_commands.Group(name="market", description="Trade raw and smelted materials with the server")

    async def _listable_autocomplete(self, interaction: discord.Interaction, current: str):
        """What this player could put up for sale: the material stacks they
        actually hold, then their free drills.

        An autocomplete rather than a choice list because Discord caps a static
        choice list at 25 and there are 22 tradeable material ids before a
        single drill is counted. It offers only what they HAVE, which is also
        what makes it useful - the alternative is a list of 22 things they
        mostly can't sell.

        Drills encode as "drill:<id>" because a drill is not a stack: two Steel
        Drills differ by level and container, so the row has to be named rather
        than the type. PERMANENT_MATERIALS never appears (data/materials.py).
        """
        search = current.strip().lower()
        choices = []

        rows = await self.db.fetchall(
            "SELECT material_id, quantity FROM user_materials "
            "WHERE user_id = ? AND quantity > 0",
            (interaction.user.id,),
        )
        held = {row["material_id"]: row["quantity"] for row in rows}
        for material_id in TRADEABLE_ORDER + tuple(
            m for m in ALL_MATERIALS if m not in TRADEABLE_ORDER
        ):
            if material_id not in held or material_id in PERMANENT_MATERIALS:
                continue
            if material_id in DRILLS:
                continue
            info = ALL_MATERIALS[material_id]
            label = f"{info['name']} ({held[material_id]:,})"
            if search and search not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label, value=material_id))
            if len(choices) >= MAX_AUTOCOMPLETE_RESULTS:
                return choices

        drills = await drill_choices(
            self.db, interaction.user.id, current,
            scope=DrillScope.UNPLACED, bot=self.bot,
        )
        for choice in drills:
            choices.append(app_commands.Choice(name=choice.name, value=f"drill:{choice.value}"))
            if len(choices) >= MAX_AUTOCOMPLETE_RESULTS:
                break
        return choices

    @staticmethod
    def _offer_choice(material_id: str, quantity: int, price_units: int, search: str):
        """One autocomplete entry for a material somebody can actually trade
        right now, labelled with how much is on offer and at what price.

        The figures are the point of the list rather than decoration: an
        autocomplete that named a material without them would send the player
        to run the command to find out whether it was worth running.
        """
        info = ALL_MATERIALS[material_id]
        label = (
            f"{info['name']} - {quantity:,} at "
            f"{format_price(player_price_total(price_units, 1))}"
        )
        if search and search not in label.lower():
            return None
        return app_commands.Choice(name=label[:100], value=material_id)

    async def _buyable_autocomplete(self, interaction: discord.Interaction, current: str):
        """What /market buy can actually fill right now: materials with stock
        behind them, then the drills listed in this server.

        Deliberately NOT the whole material table. /market buy is
        all-or-nothing against both books, so a material nobody has listed and
        the server does not hold is a choice whose only possible outcome is
        "Only 0 of that is for sale". Offering it is offering a dead end - and
        with sixteen of the twenty-two materials being ones the server never
        stocks, most of the list would have been dead ends on a quiet server.

        The price shown is the CHEAPEST source, which is what plan_buy will
        fill from first: a player listing where one undercuts the server (the
        band rule guarantees it does), otherwise the server's own ask.

        Drills appear here and not in _orderable_autocomplete because the two
        answer different questions. You cannot BID for a drill - an order names
        a kind of thing, and a drill is never just its kind - but you can buy
        one somebody has put up, because that listing names the specific drill
        with its level and container. The value is the LISTING id, not the
        drill id: what is being accepted is the offer.
        """
        search = current.strip().lower()
        choices = []

        listed = await listed_quantities(self.db, interaction.guild_id, interaction.user.id)
        stocks = await get_server_stocks(self.db, interaction.guild_id)
        for material_id in ORDERABLE_MATERIALS:
            quantity, price_units = listed.get(material_id, (0, None))
            stock = stocks.get(material_id, 0) if material_id in TRADEABLE_ORDER else 0
            if stock:
                quantity += stock
                server_ask = server_price_units(material_id, buying=True)
                price_units = min(price_units, server_ask) if price_units else server_ask
            if not quantity:
                continue
            choice = self._offer_choice(material_id, quantity, price_units, search)
            if choice is None:
                continue
            choices.append(choice)
            if len(choices) >= MAX_AUTOCOMPLETE_RESULTS:
                return choices

        rows = await self.db.fetchall(
            "SELECT l.listing_id, l.price_units, d.* FROM market_listings l "
            "JOIN drills d ON d.drill_id = l.drill_id "
            "WHERE l.guild_id = ? AND l.seller_id != ? "
            "ORDER BY l.price_units ASC",
            (interaction.guild_id, interaction.user.id),
        )
        for row in rows:
            label = f"{drill_short_label(row)}"
            if row["container_type"]:
                label += f" \u00b7 {container_name(row['container_type'])}"
            label += f" - {format_price(player_price_total(row['price_units'], 1))}"
            if search and search not in label.lower():
                continue
            choices.append(
                app_commands.Choice(
                    name=label[:100], value=f"{LISTING_VALUE_PREFIX}{row['listing_id']}"
                )
            )
            if len(choices) >= MAX_AUTOCOMPLETE_RESULTS:
                break
        return choices

    async def _sellable_autocomplete(self, interaction: discord.Interaction, current: str):
        """What /market sell can actually move: things the player HOLDS that
        somebody here will buy.

        Two filters, and both matter. Holding it is the obvious one - the
        command's first rejection is "you only have N of that", and a list of
        things you own none of is a list of that rejection. Having a buyer is
        the less obvious one: the server bids for the six materials in
        TRADEABLE_ORDER and for nothing else, so a player sitting on rubies
        with no standing bid in the server genuinely cannot sell them, and
        saying so by omission beats saying so after the command runs.

        The price shown is the DEAREST source, which is what plan_sell fills
        into first - a player's bid where one beats the server, otherwise the
        server's own flat rate.
        """
        search = current.strip().lower()
        choices = []

        rows = await self.db.fetchall(
            "SELECT material_id, quantity FROM user_materials "
            "WHERE user_id = ? AND quantity > 0",
            (interaction.user.id,),
        )
        held = {r["material_id"]: r["quantity"] for r in rows}
        bids = await bid_quantities(self.db, interaction.guild_id, interaction.user.id)

        for material_id in ORDERABLE_MATERIALS:
            quantity = held.get(material_id, 0)
            if not quantity:
                continue
            _, price_units = bids.get(material_id, (0, None))
            if material_id in TRADEABLE_ORDER:
                server_bid = server_price_units(material_id, buying=False)
                price_units = max(price_units, server_bid) if price_units else server_bid
            if price_units is None:
                # Held, but nobody is buying: not in TRADEABLE_ORDER and no
                # standing bid. /market list is how this one finds a buyer.
                continue
            choice = self._offer_choice(material_id, quantity, price_units, search)
            if choice is None:
                continue
            choices.append(choice)
            if len(choices) >= MAX_AUTOCOMPLETE_RESULTS:
                break
        return choices

    async def _orderable_autocomplete(self, interaction: discord.Interaction, current: str):
        """Anything a player may bid for: every material id except Exotic
        Matter and the drill types.

        No drill orders. An order names a KIND of thing, and a drill is never
        just its kind - level and container are most of what one is worth - so
        a bid for "a Steel Drill" is not a well-formed offer. Drills are sold
        by listing the specific one (see _listable_autocomplete).
        """
        search = current.strip().lower()
        choices = []
        for material_id in ORDERABLE_MATERIALS:
            info = ALL_MATERIALS[material_id]
            if search and search not in info["name"].lower():
                continue
            choices.append(app_commands.Choice(name=info["name"], value=material_id))
            if len(choices) >= MAX_AUTOCOMPLETE_RESULTS:
                break
        return choices


    @market_group.command(name="sell", description="Sell from your inventory, to players or to the server")
    @app_commands.describe(item="What to sell", quantity="How many to sell")
    @app_commands.autocomplete(item=_sellable_autocomplete)
    async def market_sell(self, interaction: discord.Interaction, item: str, quantity: app_commands.Range[int, 1, MAX_MARKET_QUANTITY]):
        # An autocomplete rather than the six static choices this had until
        # 1.4. Players bid for things the server never touches - gemstones,
        # components, containers - and a sale into one of those bids is still a
        # sale, so the list is built from what this player holds and what
        # somebody here will buy, not from TRADEABLE_ORDER.
        #
        # The list is a convenience, not the enforcement: a submitted value
        # need never have come from it (see utils/drills.py's module
        # docstring), so the membership check below is what actually holds.
        if item not in ORDERABLE_MATERIALS:
            await interaction.response.send_message(f"Unknown item `{item}`.", ephemeral=True)
            return
        info = ALL_MATERIALS[item]

        # Read the member count before opening the transaction: it can chunk
        # the guild over the gateway, and holding the write lock across a
        # network round-trip would stall every other command in the bot. The
        # price no longer needs it; the job board still does, to weigh which
        # material a server of this size is short of.
        member_count = await human_member_count(interaction.guild)

        # The payout no longer depends on anything readable - the price is a
        # constant - but the INVENTORY still has to be checked and deducted
        # atomically. Run twice concurrently without a transaction and both
        # invocations pass the "do you have enough" check against the same
        # quantity, and the player is paid twice for one stack. Since 1.4 the
        # BOOKS have to be read in here too: a standing bid this plan is about
        # to fill can be filled by somebody else between the read and the write.
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_server_row(tx, interaction.guild_id)

                # Post the day's job BEFORE this sale moves the stock. Its
                # size and bonus no longer depend on stock at all, but WHICH
                # material it picks still does - so posting it afterwards
                # would let whoever sells first each day steer the board away
                # from what they had just delivered. Idempotent, so the
                # credit_job_progress call below finds this same row.
                await ensure_todays_job(tx, interaction.guild_id, member_count)

                have = await get_user_quantity(tx, interaction.user.id, item)
                if have < quantity:
                    await interaction.response.send_message(
                        f"You only have {have} of that item.", ephemeral=True
                    )
                    return

                # Dearest bid first, then the server. A player bidding above
                # the server's flat rate is the only reason to prefer them, and
                # the band rule (utils/market_book.py) is what guarantees any
                # bid on the book does.
                fills, short = await plan_sell(
                    tx, interaction.guild_id, interaction.user.id, item, quantity
                )
                if short:
                    # All-or-nothing, exactly as /market buy is. The shortfall
                    # MUST be checked: the deduction below takes the full
                    # quantity whatever the plan covers, so ignoring it
                    # destroyed the unsold remainder and paid nothing for it.
                    #
                    # It can only be nonzero for a material outside
                    # TRADEABLE_ORDER, since the server's appetite is unlimited
                    # for the six it trades - so the player is holding
                    # something only another player would buy, and nobody
                    # currently is. /market list is what that case needs.
                    sellable = quantity - short
                    if sellable:
                        await interaction.response.send_message(
                            f"Only {sellable:,} of that is being bid for right now. "
                            f"`/market list` puts the rest up at your own price.",
                            ephemeral=True,
                        )
                    else:
                        await interaction.response.send_message(
                            f"Nobody is buying {info['name']} right now, and the server "
                            f"doesn't trade it. `/market list` puts it up at your own price.",
                            ephemeral=True,
                        )
                    return

                total_value = fills_total(fills)
                to_server = server_quantity(fills)

                await deduct_user_quantity(tx, interaction.user.id, item, quantity)

                for fill in fills:
                    if fill.source is SERVER:
                        # The only leg that mints. The server hands over new
                        # currency for goods that enter its storage.
                        await adjust_server_stock(tx, interaction.guild_id, item, fill.quantity)
                        await record_minted(tx, interaction.guild_id, fill.total)
                    else:
                        # A player leg mints nothing: the buyer escrowed this
                        # currency when they placed the bid, and it moves from
                        # that escrow to the seller. The goods go straight to
                        # the buyer.
                        await consume_order(tx, fill.row_id, fill.quantity)
                        await adjust_user_quantity(tx, fill.counterparty, item, fill.quantity)
                    await adjust_currency_balance(
                        tx, interaction.guild_id, interaction.user.id, fill.total
                    )

                # ONLY the server's share. A player-to-player sale does not
                # remove goods from the player sector, so crediting the board
                # for one would let two players pass a stack back and forth and
                # mint JOB_BOARD_TARGET_PAYOUT on every leg out of nothing -
                # see the module docstring in utils/market_book.py and the
                # bullets under JOB_BOARD_TARGET_PAYOUT in data/materials.py.
                # to_server is 0 on a sale that cleared entirely against bids,
                # and credit_job_progress is a no-op at 0.
                bonus, completions = await credit_job_progress(
                    tx, interaction.guild_id, interaction.user.id,
                    item, to_server, member_count,
                )

                # Read inside the same transaction so the receipt's totals
                # can never be stale relative to the writes above.
                remaining = await get_user_quantity(tx, interaction.user.id, item)
                new_balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your inventory or one of those bids changed while that was going through - "
                "nothing was sold. Try again.",
                ephemeral=True,
            )
            return

        currency_emoji = await self._get_currency_emoji(interaction.guild_id)
        description = (
            f"Sold {info['emoji']} **{quantity:,}x {info['name']}** for "
            f"{format_currency(total_value, currency_emoji)}."
        )
        counterparty_lines = fill_lines(fills, currency_emoji, "to")
        if counterparty_lines:
            description += "\n" + "\n".join(counterparty_lines)
        if completions > 0:
            # A second line on the same description rather than a separate
            # field - one sale triggering two payouts is a single event, not
            # two, and this keeps the receipt's shape identical to /market
            # buy's even when a job board bonus lands.
            times = "" if completions == 1 else f" **{completions:,}** times"
            description += (
                f"\nThat finished today's job board task{times} for a bonus of "
                f"{format_currency(bonus, currency_emoji)}."
            )
        embed = build_market_receipt_embed(
            title="🪙 Sale Receipt",
            color=MARKET_COLOR,
            description=description,
            material_field="Sold",
            material_id=item,
            quantity=quantity,
            material_remaining=remaining,
            material_gained=False,
            currency_field="Received",
            # Includes the job board bonus, if one landed - balance_after
            # already reflects both credits, and this field is the amount
            # that moved to get there, not just the sale's own half of it.
            currency_amount=total_value + bonus,
            balance_after=new_balance,
            currency_gained=True,
            currency_emoji=currency_emoji,
            round_up_currency=False,
        )
        await respond(interaction, self.db, embed=embed)

    @market_group.command(name="buy", description="Buy from player listings and the server's stock")
    @app_commands.describe(item="What to buy", quantity="How many (ignored for a drill)")
    @app_commands.autocomplete(item=_buyable_autocomplete)
    async def market_buy(self, interaction: discord.Interaction, item: str, quantity: app_commands.Range[int, 1, MAX_MARKET_QUANTITY] = 1):
        # A listed drill is bought by accepting its listing rather than by
        # asking for a quantity of a type - see _buyable_autocomplete.
        if item.startswith(LISTING_VALUE_PREFIX):
            await self._buy_listed_drill(interaction, item)
            return
        # Widened from the six static choices this had until 1.4: players list
        # things the server never stocks, and buying one of those is still a
        # purchase. The autocomplete offers only what is actually on sale;
        # this is the check that holds, since a submitted value need never
        # have come from it.
        if item not in ORDERABLE_MATERIALS:
            await interaction.response.send_message(f"Unknown item `{item}`.", ephemeral=True)
            return
        info = ALL_MATERIALS[item]

        # This hits Discord, so it happens before the write lock is taken -
        # see the note in market_sell.
        currency_emoji = await self._get_currency_emoji(interaction.guild_id)

        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_server_row(tx, interaction.guild_id)

                # Cheapest source first. A player listing is always under the
                # server's ask by the band rule (utils/market_book.py), so in
                # practice the book clears before the shelves do - but the plan
                # is ordered by price rather than by source, so it stays right
                # for the sixteen materials the server does not stock at all.
                fills, short = await plan_buy(
                    tx, interaction.guild_id, interaction.user.id, item, quantity
                )
                if short:
                    # All-or-nothing, as this command has always been. The
                    # figure names what BOTH books could cover between them,
                    # which is the number the player needs in order to retry.
                    available = quantity - short
                    await interaction.response.send_message(
                        f"Only {available:,} of that is for sale right now, across the "
                        f"server's stock and every player listing.",
                        ephemeral=True,
                    )
                    return

                total_cost = fills_total(fills)
                balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
                if balance < total_cost:
                    await interaction.response.send_message(
                        self._cannot_afford_message(
                            item, quantity, total_cost, balance, currency_emoji,
                        ),
                        ephemeral=True,
                    )
                    return

                await deduct_currency_balance(tx, interaction.guild_id, interaction.user.id, total_cost)

                for fill in fills:
                    if fill.source is SERVER:
                        # The only leg that burns. Currency paid to the server
                        # leaves circulation entirely (docs/market.md
                        # section 3) and its stock falls by what it sold.
                        await deduct_server_stock(tx, interaction.guild_id, item, fill.quantity)
                        await record_burned(tx, interaction.guild_id, fill.total)
                    else:
                        # A player leg burns nothing. The goods were escrowed
                        # out of the seller's inventory when they listed them,
                        # so only the currency moves here.
                        await consume_listing(tx, fill.row_id, fill.quantity)
                        await adjust_currency_balance(
                            tx, interaction.guild_id, fill.counterparty, fill.total
                        )
                    await adjust_user_quantity(tx, interaction.user.id, item, fill.quantity)

                # Read inside the same transaction so the receipt's totals
                # can never be stale relative to the writes above.
                remaining = await get_user_quantity(tx, interaction.user.id, item)
                balance_after = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "The stock on offer or your balance changed while that was going through - "
                "nothing was bought. Try again.",
                ephemeral=True,
            )
            return

        description = (
            f"Bought {info['emoji']} **{quantity:,}x {info['name']}** for "
            f"{format_currency(total_cost, currency_emoji, True)}."
        )
        counterparty_lines = fill_lines(fills, currency_emoji, "from")
        if counterparty_lines:
            description += "\n" + "\n".join(counterparty_lines)

        embed = build_market_receipt_embed(
            title="🛒 Purchase Receipt",
            color=MARKET_COLOR,
            description=description,
            material_field="Bought",
            material_id=item,
            quantity=quantity,
            material_remaining=remaining,
            material_gained=True,
            currency_field="Spent",
            currency_amount=total_cost,
            balance_after=balance_after,
            currency_gained=False,
            currency_emoji=currency_emoji,
            round_up_currency=True,
        )
        await respond(interaction, self.db, embed=embed)

    # ------------------------------------------------------------------
    # The player market (1.4): /market list, /market order, /market cancel
    # ------------------------------------------------------------------

    @market_group.command(name="list", description="Offer something from your inventory to other players")
    @app_commands.describe(
        item="What to sell", quantity="How many (ignored for a drill)", price="Asking price per unit",
    )
    @app_commands.autocomplete(item=_listable_autocomplete)
    async def market_list(
        self,
        interaction: discord.Interaction,
        item: str,
        price: app_commands.Range[float, 0.0001, MAX_MARKET_PRICE],
        quantity: app_commands.Range[int, 1, MAX_MARKET_QUANTITY] = 1,
    ):
        price_units = player_price_units(price)
        if price_units is None:
            await interaction.response.send_message(
                f"Prices go down to {SMALLEST_PRICE} and no further. "
                f"Round that to the nearest {SMALLEST_PRICE} and try again.",
                ephemeral=True,
            )
            return

        # A drill listing and a material listing validate almost nothing in
        # common, so they split here rather than threading a flag through one
        # body. The value need never have come from the autocomplete we offered
        # (utils/drills.py module docstring), so both halves re-validate.
        if item.startswith(DRILL_VALUE_PREFIX):
            await self._list_drill(interaction, item, price_units)
            return
        await self._list_material(interaction, item, quantity, price_units)

    async def _buy_listed_drill(self, interaction: discord.Interaction, value: str):
        """Accepts a drill listing: the drill changes owner, the seller is
        paid, and the listing goes away.

        The drill keeps its level, container and identity - it is the same
        drills row throughout, which is the whole reason a drill is listed by
        id rather than sold as a stack. Nothing is minted or burned; this is a
        transfer between two players.
        """
        try:
            listing_id = int(value[len(LISTING_VALUE_PREFIX):])
        except ValueError:
            await interaction.response.send_message(f"Unknown item `{value}`.", ephemeral=True)
            return

        currency_emoji = await self._get_currency_emoji(interaction.guild_id)
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_server_row(tx, interaction.guild_id)

                listing = await tx.fetchone(
                    "SELECT * FROM market_listings WHERE listing_id = ? AND guild_id = ?",
                    (listing_id, interaction.guild_id),
                )
                if listing is None or listing["drill_id"] is None:
                    await interaction.response.send_message(
                        "That listing is gone - somebody else took it, or it was withdrawn.",
                        ephemeral=True,
                    )
                    return
                if listing["seller_id"] == interaction.user.id:
                    await interaction.response.send_message(
                        "That's your own listing. `/market cancel` takes it back.",
                        ephemeral=True,
                    )
                    return

                cost = player_price_total(listing["price_units"], 1)
                balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
                if balance < cost:
                    await interaction.response.send_message(
                        f"That costs {format_currency(cost, currency_emoji, True)}, but you "
                        f"only have {format_currency(balance, currency_emoji)}.",
                        ephemeral=True,
                    )
                    return

                row = await tx.fetchone(
                    "SELECT * FROM drills WHERE drill_id = ?", (listing["drill_id"],)
                )
                await deduct_currency_balance(tx, interaction.guild_id, interaction.user.id, cost)
                await adjust_currency_balance(tx, interaction.guild_id, listing["seller_id"], cost)
                # Guarded on listed_id so two buyers inside one write lock
                # cannot both claim it - the same shape as the claim that
                # listed it in the first place.
                claimed = await tx.execute_changes(
                    "UPDATE drills SET owner_id = ?, listed_id = NULL "
                    "WHERE drill_id = ? AND listed_id = ?",
                    (interaction.user.id, listing["drill_id"], listing_id),
                )
                if not claimed:
                    raise InsufficientQuantity(f"drill listing {listing_id} was taken")
                await tx.execute(
                    "DELETE FROM market_listings WHERE listing_id = ?", (listing_id,)
                )
                balance_after = await get_currency_balance(
                    tx, interaction.guild_id, interaction.user.id
                )
        except InsufficientQuantity:
            await interaction.response.send_message(
                "That listing was taken while this was going through - nothing was bought.",
                ephemeral=True,
            )
            return

        embed = make_embed("\U0001F6D2 Purchase Receipt", MARKET_COLOR, description=(
            f"Bought **{drill_label(row)}** from <@{listing['seller_id']}> for "
            f"{format_currency(cost, currency_emoji, True)}.\n"
            f"Your balance is {format_currency(balance_after, currency_emoji)}."
        ))
        await respond(interaction, self.db, embed=embed)

    async def _list_drill(self, interaction: discord.Interaction, item: str, price_units: int):
        try:
            drill_id = int(item[len(DRILL_VALUE_PREFIX):])
        except ValueError:
            await interaction.response.send_message(f"Unknown item `{item}`.", ephemeral=True)
            return

        currency_emoji = await self._get_currency_emoji(interaction.guild_id)
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_server_row(tx, interaction.guild_id)

                row = await fetch_drill(tx, drill_id, interaction.user.id)
                if row is None:
                    await interaction.response.send_message(
                        "You don't own that drill.", ephemeral=True
                    )
                    return
                if row["guild_id"] is not None:
                    await interaction.response.send_message(
                        f"**{drill_label(row)}** is placed in a server - retract it before "
                        f"you list it.",
                        ephemeral=True,
                    )
                    return
                unavailable = drill_unavailable_message(row, "list it")
                if unavailable is not None:
                    await interaction.response.send_message(unavailable, ephemeral=True)
                    return

                listing_id = await tx.execute(
                    "INSERT INTO market_listings "
                    "(guild_id, seller_id, drill_id, quantity, price_units) VALUES (?, ?, ?, 1, ?)",
                    (interaction.guild_id, interaction.user.id, drill_id, price_units),
                )
                # Insert first, then claim, so listed_id always names a row
                # that exists - the same ordering /factory upgrade uses when it
                # queues a job before taking the drill's lock. The WHERE guard
                # is what makes the claim atomic rather than merely checked.
                claimed = await tx.execute_changes(
                    f"UPDATE drills SET listed_id = ? WHERE drill_id = ? AND {DRILL_AVAILABLE_SQL}",
                    (listing_id, drill_id),
                )
                if not claimed:
                    raise InsufficientQuantity(f"drill {drill_id} was taken while listing")
        except InsufficientQuantity:
            await interaction.response.send_message(
                "That drill was claimed by something else while this was going through - "
                "nothing was listed. Try again.",
                ephemeral=True,
            )
            return

        embed = make_embed("🏷️ Listed", MARKET_COLOR, description=(
            f"**{drill_label(row)}** is up for sale at "
            f"{format_currency(player_price_total(price_units, 1), currency_emoji)}.\n"
            f"Take it back any time with `/market cancel {listing_id}`."
        ))
        await respond(interaction, self.db, embed=embed)

    async def _list_material(
        self, interaction: discord.Interaction, item: str, quantity: int, price_units: int
    ):
        info = get_material_info(item)
        if info is None or item in DRILLS:
            await interaction.response.send_message(f"Unknown item `{item}`.", ephemeral=True)
            return

        blocked = permanent_material_error(item) or listing_price_error(item, price_units)
        if blocked:
            await interaction.response.send_message(blocked, ephemeral=True)
            return

        currency_emoji = await self._get_currency_emoji(interaction.guild_id)
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_server_row(tx, interaction.guild_id)

                have = await get_user_quantity(tx, interaction.user.id, item)
                if have < quantity:
                    await interaction.response.send_message(
                        f"You only have {have:,} of that item.", ephemeral=True
                    )
                    return

                # The goods are ESCROWED: they leave the inventory now and live
                # on the listing until it fills or is cancelled. Without this a
                # player could list a stack and then sell the same stack to the
                # server, leaving a listing promising goods that had gone.
                await deduct_user_quantity(tx, interaction.user.id, item, quantity)
                listing_id = await tx.execute(
                    "INSERT INTO market_listings "
                    "(guild_id, seller_id, material_id, quantity, price_units) VALUES (?, ?, ?, ?, ?)",
                    (interaction.guild_id, interaction.user.id, item, quantity, price_units),
                )
                remaining = await get_user_quantity(tx, interaction.user.id, item)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your inventory changed while that was going through - nothing was listed. "
                "Try again.",
                ephemeral=True,
            )
            return

        embed = make_embed("🏷️ Listed", MARKET_COLOR, description=(
            f"{info['emoji']} **{quantity:,}x {info['name']}** is up for sale at "
            f"{format_currency(player_price_total(price_units, 1), currency_emoji)} each "
            f"({format_currency(player_price_total(price_units, quantity), currency_emoji)} the lot).\n"
            f"You have **{remaining:,}** left. Take the listing back any time with "
            f"`/market cancel {listing_id}`."
        ))
        await respond(interaction, self.db, embed=embed)

    @market_group.command(name="order", description="Bid for anything, at a price you set")
    @app_commands.describe(
        item="What to buy", quantity="How many", price="What you'll pay per unit",
    )
    @app_commands.autocomplete(item=_orderable_autocomplete)
    async def market_order(
        self,
        interaction: discord.Interaction,
        item: str,
        quantity: app_commands.Range[int, 1, MAX_MARKET_QUANTITY],
        price: app_commands.Range[float, 0.0001, MAX_MARKET_PRICE],
    ):
        price_units = player_price_units(price)
        if price_units is None:
            await interaction.response.send_message(
                f"Prices go down to {SMALLEST_PRICE} and no further. "
                f"Round that to the nearest {SMALLEST_PRICE} and try again.",
                ephemeral=True,
            )
            return

        # Exotic Matter FIRST, before the membership check. ORDERABLE_MATERIALS
        # excludes it by construction, so without this a player who typed it
        # got "Unknown item" - which reads as a typo they should correct rather
        # than as a rule they can't get around. It is checked on this command
        # and not only on /market list because a bid nobody is permitted to
        # fill would sit on the book holding the buyer's currency in escrow
        # against a trade that can never happen.
        blocked = permanent_material_error(item)
        if blocked:
            await interaction.response.send_message(blocked, ephemeral=True)
            return

        if item not in ORDERABLE_MATERIALS:
            await interaction.response.send_message(f"Unknown item `{item}`.", ephemeral=True)
            return

        blocked = order_price_error(item, price_units)
        if blocked:
            await interaction.response.send_message(blocked, ephemeral=True)
            return

        info = get_material_info(item)
        total = player_price_total(price_units, quantity)
        currency_emoji = await self._get_currency_emoji(interaction.guild_id)

        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_server_row(tx, interaction.guild_id)

                balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
                if balance < total:
                    await interaction.response.send_message(
                        f"That order would cost {format_currency(total, currency_emoji, True)}, "
                        f"but you only have {format_currency(balance, currency_emoji)}.",
                        ephemeral=True,
                    )
                    return

                # The currency is ESCROWED, for the same reason a listing's
                # goods are: an order promising money the buyer has since spent
                # is an order that cannot be filled. This is NOT a burn -
                # nothing was destroyed and cancelling returns every unit - so
                # record_burned is deliberately not called here, and
                # circulating_currency adds this back (docs/market.md 4).
                await deduct_currency_balance(tx, interaction.guild_id, interaction.user.id, total)
                order_id = await tx.execute(
                    "INSERT INTO market_orders "
                    "(guild_id, buyer_id, material_id, quantity, price_units) VALUES (?, ?, ?, ?, ?)",
                    (interaction.guild_id, interaction.user.id, item, quantity, price_units),
                )
                balance_after = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your balance changed while that was going through - no order was placed. "
                "Try again.",
                ephemeral=True,
            )
            return

        embed = make_embed("📋 Order Placed", MARKET_COLOR, description=(
            f"Bidding {format_currency(player_price_total(price_units, 1), currency_emoji)} each for "
            f"{info['emoji']} **{quantity:,}x {info['name']}**.\n"
            f"{format_currency(total, currency_emoji, True)} is held until it fills; "
            f"your balance is {format_currency(balance_after, currency_emoji)}.\n"
            f"Withdraw it any time with `/market cancel {order_id}`."
        ))
        await respond(interaction, self.db, embed=embed)

    @market_group.command(name="cancel", description="Withdraw one of your listings or orders")
    @app_commands.describe(id="The listing or order number from its receipt")
    async def market_cancel(self, interaction: discord.Interaction, id: int):
        """Withdraws a listing or an order and returns whatever it was holding.

        One command for both books rather than two, because a player thinks of
        these as "the thing I put up" rather than as two kinds of row, and the
        ids cannot collide in practice for the person typing one: they only
        ever get an id from their own receipt, and this refuses anything that
        isn't theirs either way.

        Cancellation is not optional scope. Escrow means a listing holds real
        goods and an order holds real currency, so a book entry that could not
        be withdrawn would be a permanent hole in somebody's inventory.
        """
        currency_emoji = await self._get_currency_emoji(interaction.guild_id)
        try:
            async with self.db.transaction() as tx:
                listing = await tx.fetchone(
                    "SELECT * FROM market_listings WHERE listing_id = ? AND seller_id = ? AND guild_id = ?",
                    (id, interaction.user.id, interaction.guild_id),
                )
                if listing is not None:
                    description = await self._cancel_listing(tx, listing, currency_emoji)
                else:
                    order = await tx.fetchone(
                        "SELECT * FROM market_orders WHERE order_id = ? AND buyer_id = ? AND guild_id = ?",
                        (id, interaction.user.id, interaction.guild_id),
                    )
                    if order is None:
                        await interaction.response.send_message(
                            f"You have no listing or order numbered {id} in this server.",
                            ephemeral=True,
                        )
                        return
                    description = await self._cancel_order(tx, order, currency_emoji)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "That filled while this was going through - nothing was cancelled.",
                ephemeral=True,
            )
            return

        embed = make_embed("↩️ Withdrawn", MARKET_COLOR, description=description)
        await respond(interaction, self.db, embed=embed)

    async def _cancel_listing(self, tx, listing, currency_emoji) -> str:
        """Returns a listing's escrowed goods and deletes it."""
        await tx.execute("DELETE FROM market_listings WHERE listing_id = ?", (listing["listing_id"],))
        if listing["drill_id"] is not None:
            # Matching on listed_id makes this idempotent, the same property
            # cogs/factory.py relies on when it releases an upgraded drill.
            await tx.execute(
                "UPDATE drills SET listed_id = NULL WHERE drill_id = ? AND listed_id = ?",
                (listing["drill_id"], listing["listing_id"]),
            )
            row = await fetch_drill(tx, listing["drill_id"], listing["seller_id"])
            return f"**{drill_label(row)}** is back in your inventory."

        await adjust_user_quantity(tx, listing["seller_id"], listing["material_id"], listing["quantity"])
        info = get_material_info(listing["material_id"])
        return (
            f"{info['emoji']} **{listing['quantity']:,}x {info['name']}** is back in your "
            f"inventory."
        )

    async def _cancel_order(self, tx, order, currency_emoji) -> str:
        """Returns an order's escrowed currency and deletes it.

        adjust_currency_balance rather than record_minted: this currency was
        never burned when it was escrowed, so returning it mints nothing.
        """
        await tx.execute("DELETE FROM market_orders WHERE order_id = ?", (order["order_id"],))
        refund = player_price_total(order["price_units"], order["quantity"])
        await adjust_currency_balance(tx, order["guild_id"], order["buyer_id"], refund)
        info = get_material_info(order["material_id"])
        return (
            f"Your bid for {info['emoji']} **{order['quantity']:,}x {info['name']}** is "
            f"withdrawn. {format_currency(refund, currency_emoji)} is back in your balance."
        )

    @market_group.command(name="status", description="Show the server's current market prices")
    async def market_status(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        currency_emoji = await self._get_currency_emoji(interaction.guild_id) or DEFAULT_CURRENCY_EMOJI

        # One line per material - {emoji} {sell price} {buy price} - instead
        # of three parallel Material/Sell/Buy columns split across separate
        # fields. The material name is deliberately left out: it's
        # proportional-width text, so keeping it would shift the price columns
        # per line and defeat the alignment. Only the material emoji precedes
        # the prices, and custom Discord emoji all render at a fixed size
        # unlike text.
        #
        # The currency emoji is named once in the heading rather than twice on
        # every line, as it is on the player books below. It is the same emoji
        # on all of them, and a server whose emoji is a long custom or animated
        # one was paying for it per price: with both books full, repeating it
        # here and on every book line put the whole embed past Discord's 6,000
        # character ceiling, which fails the message rather than truncating it.
        # tests/test_player_market.py: EmbedBudgetTests pins the worst case.
        #
        # THE SERVER's prices need nothing but format_price. Every one is a
        # whole number of cents under a single currency unit, so they are all
        # four characters wide on their own and these columns line up for free.
        # A material priced at 1.00 or more would simply take one more
        # character than its neighbours, which costs a line of alignment rather
        # than breaking anything.
        #
        # That argument covers this field and no longer covers the whole embed.
        # It is why the fixed five-significant-digit formatter was deleted in
        # 1.3 when prices went static - and why 1.4 brought it back for the two
        # player-book fields below, where a price CAN be any magnitude and CAN
        # be worth a small fraction of a cent. See utils/formatting.py:
        # format_compact_price, which is the same function restored.
        lines = []

        # The prices are the same on every server and every day (1.3), so the
        # only thing read per material is the stock - the server can only sell
        # back what it is holding - and all of it comes from one query rather
        # than one per material.
        stocks = await get_server_stocks(self.db, interaction.guild_id)
        for material_id, info in TRADEABLE_MATERIALS.items():
            current_stock = stocks.get(material_id, 0)
            # SELL = what you receive per unit selling to the server (/market sell).
            # BUY = what you pay per unit buying from the server (/market buy).
            sell_str = f"`{format_price(sale_unit_price(material_id))}`"
            if current_stock > 0:
                buy_str = (
                    f"`{format_price(purchase_unit_price(material_id))}` "
                    f"({current_stock:,} in stock)"
                )
            else:
                buy_str = "`N/A`"
            lines.append(f"{info['emoji']} {sell_str} {buy_str}")

        embed = make_embed("Server Market", MARKET_COLOR)
        # What you can actually spend, next to what everything costs - the two
        # numbers are only useful together, and /balance is a separate command.
        balance = await get_currency_balance(self.db, interaction.guild_id, interaction.user.id)
        embed.description = (
            f"Your balance: {format_currency(balance, currency_emoji)}"
        )
        add_multi_field(embed, f"Item · Sell · Buy · {currency_emoji} each", lines)

        # The player books. Aggregated per material rather than listed row by
        # row - see utils/market_book.py: listing_depth for why, and for why
        # that is also what bounds these fields.
        listings = await listing_depth(self.db, interaction.guild_id)
        drills = await listed_drills(self.db, interaction.guild_id)
        orders = await order_depth(self.db, interaction.guild_id)

        add_multi_field(
            embed, f"Market Listings · {currency_emoji} each",
            self._depth_lines(listings, "seller")
            + self._drill_lines(drills),
            empty_text="Nobody is selling anything. `/market list` to be the first.",
        )
        add_multi_field(
            embed, f"Market Orders · {currency_emoji} each",
            self._depth_lines(orders, "buyer"),
            empty_text="No open bids. `/market order` to place one.",
        )

        await respond(interaction, self.db, embed=embed)

    @market_group.command(name="entries", description="Your own open listings and orders")
    async def market_entries(self, interaction: discord.Interaction):
        """Everything this player has on the server's books, with the ids
        /market cancel takes.

        Its own page rather than a field on /market status. The two answer
        different questions - "what is the market doing" against "what am I
        committed to" - and only the second is worth a per-viewer database read
        on a command everybody runs to check prices. It also lifts the display
        cap: a field sharing an embed with four others could name ten entries,
        where a page of its own can name ENTRIES_DISPLAY_LIMIT.

        The id matters because it is what /market cancel takes, and the only
        other place one appears is the receipt from when the entry was made.
        """
        await ensure_server_row(self.db, interaction.guild_id)
        currency_emoji = await self._get_currency_emoji(interaction.guild_id) or DEFAULT_CURRENCY_EMOJI
        listings, orders = await own_entries(
            self.db, interaction.guild_id, interaction.user.id
        )

        embed = make_embed("Your Market Entries", MARKET_COLOR)
        balance = await get_currency_balance(self.db, interaction.guild_id, interaction.user.id)
        description = [f"Your balance: {format_currency(balance, currency_emoji)}"]
        if orders:
            # What the bids are holding. It has left the balance above but not
            # the economy (docs/market.md section 4), and a player who has
            # forgotten an old bid has no other way to see where the money
            # went.
            escrowed = sum(
                player_price_total(row["price_units"], row["quantity"]) for row in orders
            )
            description.append(
                f"Held in bids: {format_currency(escrowed, currency_emoji)}"
            )
        embed.description = "\n".join(description)

        if not listings and not orders:
            embed.add_field(
                name="Nothing on the market",
                value="`/market list` sells something of yours; `/market order` bids for something.",
                inline=False,
            )
            await respond(interaction, self.db, embed=embed)
            return

        add_multi_field(
            embed, f"Selling · {currency_emoji} each",
            self._own_listing_lines(listings), empty_text="Nothing listed.",
        )
        add_multi_field(
            embed, f"Buying · {currency_emoji} each",
            self._own_order_lines(orders), empty_text="No open bids.",
        )
        await respond(interaction, self.db, embed=embed)

    def _depth_lines(self, depth: dict, role: str) -> list[str]:
        """One line per material on one side of the book: the best price, how
        much is behind it, and how many people are offering.

        In the game's own material order (data/materials.py), like every other
        material list in the bot - not by price, which would put iron ore above
        a diamond for no reason a reader benefits from.

        format_compact_price keeps the price column one width at every
        magnitude, which is the whole reason it was restored in 1.4: these
        lines run from a sub-cent ore to a six-figure gemstone.
        """
        rank = {material_id: i for i, material_id in enumerate(ALL_MATERIALS)}
        lines = []
        for material_id in sorted(depth, key=lambda m: rank.get(m, len(rank))):
            quantity, price_units, participants = depth[material_id]
            info = get_material_info(material_id)
            price = format_compact_price(player_price_total(price_units, 1))
            who = f"{participants} {plural(role, participants)}"
            lines.append(
                f"{info['emoji']} `{price}` · {quantity:,} · {who}"
            )
        return lines

    def _drill_lines(self, rows) -> list[str]:
        """One line per listed drill. Never aggregated: each is a specific
        drill whose level and container are most of what it is worth."""
        lines = []
        for row in rows[:BOOK_DISPLAY_LIMIT]:
            label = drill_short_label(row)
            if row["container_type"]:
                label += f" · {container_name(row['container_type'])}"
            price = format_compact_price(player_price_total(row["price_units"], 1))
            lines.append(
                f"{drill_emoji(row)} `{price}` · {label} · <@{row['seller_id']}>"
            )
        if len(rows) > BOOK_DISPLAY_LIMIT:
            lines.append(f"... and {len(rows) - BOOK_DISPLAY_LIMIT:,} more drills")
        return lines

    # Every book line in this cog is the same shape: an id column where there
    # is one, then the material's own emoji, then a fixed-width price, then
    # the counts. /market status's lines are the same minus the id (see
    # _depth_lines), which is what lets a player read both pages the same way.
    #
    # The material NAME is deliberately absent, exactly as it is on
    # /market status: it is proportional-width text, so including it shifts
    # every column after it by a different amount on each line and defeats the
    # alignment format_compact_price exists to provide. The custom emoji all
    # render at one fixed size, which is why they can lead a column and a name
    # cannot.
    def _own_entry_line(self, row, price_units: int, quantity, extra: str = "") -> str:
        """One of the caller's own rows, in the shared shape.

        A drill carries its label instead of a count - its level and container
        are most of what it is worth, which is why it was listed individually
        rather than as a stack, and a bare "1" would not say which drill a
        player had put up.
        """
        if row["material_id"] is None:
            emoji = drill_emoji(row)
            count = drill_short_label(row)
            if row["container_type"]:
                count += f" · {container_name(row['container_type'])}"
        else:
            emoji = get_material_info(row["material_id"])["emoji"]
            count = f"{quantity:,}"
        price = format_compact_price(player_price_total(price_units, 1))
        return f"`#{row['id']}` {emoji} `{price}` · {count}{extra}"

    def _own_listing_lines(self, listings) -> list[str]:
        """The caller's own asks, each led by the id /market cancel takes."""
        lines = [
            self._own_entry_line(row, row["price_units"], row["quantity"])
            for row in listings[:ENTRIES_DISPLAY_LIMIT]
        ]
        if len(listings) > ENTRIES_DISPLAY_LIMIT:
            lines.append(f"... and {len(listings) - ENTRIES_DISPLAY_LIMIT:,} more")
        return lines

    def _own_order_lines(self, orders) -> list[str]:
        """The caller's own bids, with what each is holding in escrow - the
        figure a player is looking for when they wonder where their balance
        went."""
        lines = []
        for row in orders[:ENTRIES_DISPLAY_LIMIT]:
            held = format_compact_price(
                player_price_total(row["price_units"], row["quantity"])
            )
            lines.append(
                self._own_entry_line(
                    row, row["price_units"], row["quantity"], extra=f" · `{held}` held"
                )
            )
        if len(orders) > ENTRIES_DISPLAY_LIMIT:
            lines.append(f"... and {len(orders) - ENTRIES_DISPLAY_LIMIT:,} more")
        return lines

    # ------------------------------------------------------------------
    # /economy
    # ------------------------------------------------------------------

    async def _queue_lines(self, guild_id: int, cfg) -> tuple[list[str], int, int, float]:
        """One line per machine that has work outstanding, plus the totals the
        field heading carries.

        Grouped by MACHINE rather than listed job by job, which is what bounds
        this field without a JOB_DISPLAY_LIMIT and an "... and N more" line:
        there are five machines, so there are at most five lines however long
        the queue gets. The status embeds list individual jobs and do need that
        cap; this is a summary of all five at once and a list of jobs would be
        both unbounded and less use.

        The wait returned is the LONGEST of the machines', not their sum - they
        run in parallel, so the longest is when everything queued here is
        finished, which is the question the heading is answering.
        """
        jobs = await self.db.fetchall(
            "SELECT job_type, target_id, quantity FROM production_jobs "
            "WHERE guild_id = ? AND status != 'complete'",
            (guild_id,),
        )
        by_machine: dict[str, list] = {}
        for job in jobs:
            by_machine.setdefault(job["job_type"], []).append(job)

        lines: list[str] = []
        total_items = 0
        total_jobs = 0
        longest_wait = 0.0
        for machine in MACHINES:
            queued = by_machine.get(machine)
            if not queued:
                continue
            level = run_level({
                "level": cfg[f"{machine}_level"],
                "collected": cfg[f"{machine}_fees_collected"],
                "enhancement": cfg[f"{machine}_enhancement_level"],
                "bonanza_until": cfg["bonanza_until"],
            })
            # In whatever unit this machine queues in, which is batches for the
            # blast furnace and items for the other four (utils/db_helpers.py:
            # MACHINES).
            count = sum(job["quantity"] for job in queued)
            if machine == "press":
                days = sum(
                    PRESS_RECIPES[job["target_id"]]["press_days"] * job["quantity"]
                    for job in queued
                )
                wait = max(0.0, days - cfg["press_progress"]) / press_rate_per_day(level) * 24
            else:
                wait = count / MACHINE_RATE[machine](level)
            longest_wait = max(longest_wait, wait)
            total_jobs += len(queued)
            # The heading counts in items across all five, because items are the
            # only unit they share - so the blast furnace's batches are
            # converted for the total while its own line keeps the batches
            # /blast status quotes it in.
            unit = "batch" if machine == "blast_furnace" else "item"
            total_items += count * (BLAST_FURNACE_BATCH_SIZE if machine == "blast_furnace" else 1)
            lines.append(
                f"{MACHINE_EMOJI[machine]} **{machine_label(machine).capitalize()}** "
                f"{count:,} {plural(unit, count)} / {len(queued):,} {plural('job', len(queued))} "
                f"({format_duration(wait)})"
            )
        return lines, total_items, total_jobs, longest_wait

    def _gdp_windows_value(
        self, day, week, first_seen: str | None, currency_emoji: str | None
    ) -> str:
        """Both windows for /economy gdp, or an honest account of why there
        aren't any.

        first_seen is read as a flag rather than shown: a server with no ledger
        row at all gets told how to start one, since two zeroes on their own
        read as a broken command rather than as an economy nobody has used yet.
        The date itself is operator-facing and stays on the Ops dashboard
        (web/queries.py).
        """
        if first_seen is None:
            return (
                "Nothing recorded yet.\n"
                "Mine, smelt or collect something and it starts."
            )
        return (
            f"**Last 24h** {format_currency(day.gdp, currency_emoji)}\n"
            f"**Last 7d** {format_currency(week.gdp, currency_emoji)}"
        )

    def _value_breakdown_lines(self, week, currency_emoji: str | None) -> list[str]:
        """Which stage of production added the week's value, and whether this
        server's machines ran on more input than it dug up.

        The import/export sentence is the honest form of "was this smelted here
        or somewhere else". It cannot be answered per unit and never will be:
        user_materials is keyed on user_id with no guild, so once ore is in an
        inventory there is nothing that records which server it came out of.
        Comparing the aggregates is what IS answerable, and it answers the same
        question at the scale anyone actually cares about it.
        """
        lines = [
            f"{GDP_SOURCE_LABEL[source]} {format_currency(week.added_by_source[source], currency_emoji)}"
            for source in GDP_SOURCES
            if source in week.added_by_source
        ]
        if week.machine_input <= 0 and week.mined_output <= 0:
            return lines
        if week.machine_input > week.mined_output:
            verdict = "a net **importer**: its machines ran on more than it dug up"
        elif week.machine_input < week.mined_output:
            verdict = "a net **exporter**: it dug up more than its machines consumed"
        else:
            verdict = "exactly balanced"
        lines.append(
            f"Machines here consumed {format_currency(week.machine_input, currency_emoji)} "
            f"of input against {format_currency(week.mined_output, currency_emoji)} mined "
            f"here \u2014 {verdict}."
        )
        return lines

    economy_group = app_commands.Group(
        name="economy", description="This server's economy: its money, what it produces and what it owes"
    )

    @economy_group.command(
        name="status",
        description="This server's economy at a glance: wealth, fees, GDP and queues",
    )
    async def economy_status(self, interaction: discord.Interaction):
        """Read-only against balances, stock and fees - it charges nothing and
        moves nothing.

        A subcommand rather than a bare /economy because /economy gdp exists:
        Discord will not let a command with subcommands be invoked on its own,
        so the overview needs a name of its own, and `status` is the one
        /market and /mine already use for "the page that just tells you where
        this stands".

        The one write is ensure_todays_job, which posts the day's task if
        nobody has looked at the board yet. That is deliberate and is exactly
        what /jobboard does: the board is posted lazily rather than by a loop
        (utils/job_board.py), so any surface that reports it has to be willing
        to be the one that posts it.
        """
        # Both of these reach Discord, so they happen before the write lock is
        # taken - see the note on Database.transaction.
        member_count = await human_member_count(interaction.guild)
        guild_name = interaction.guild.name if interaction.guild else "This Server"

        async with self.db.transaction() as tx:
            await ensure_server_row(tx, interaction.guild_id)
            job = await ensure_todays_job(tx, interaction.guild_id, member_count)

        cfg = await self.db.fetchone(
            "SELECT * FROM server_config WHERE guild_id = ?", (interaction.guild_id,)
        )
        currency_emoji = cfg["currency_emoji"]

        # Includes what open /market order bids are holding in escrow, which
        # has left its owners' balances but not the economy - see
        # utils/db_helpers.py: circulating_currency.
        circulating = await circulating_currency_for(self.db, interaction.guild_id)

        week_cutoff = window_cutoff(GDP_WEEK_HOURS)
        week = await window_totals(self.db, interaction.guild_id, week_cutoff)
        gems = await gem_counts(self.db, interaction.guild_id, week_cutoff)

        embed = make_embed(cfg["currency_name"] or "Currency", MARKET_COLOR)
        # Unicode, not a custom <:Name:ID> - Discord renders those in
        # descriptions, field names and field values but NOT in an author line
        # (docs/stylization.md). The author line is also the only thing telling
        # this apart from /market status at a glance, since the two share
        # yellow, so it names the command outright rather than only the server.
        embed.set_author(name=f"\U0001F4CA Economy \u2022 {guild_name}")

        # The three figures that describe the whole server in one line each:
        # the money its players hold, how far it has got toward its next
        # mining slot, and what it produced this week. Slot progress is one
        # figure rather than a per-machine breakdown because which machine
        # collected which fee is the machine's own status embed's business.
        # It was "Fees collected" until 1.4 added government purchases to it
        # (utils/db_helpers.py: SLOT_PROGRESS_COLUMNS).
        #
        # The week is the one GDP window here. The 24-hour figure, the stage
        # breakdown behind it and the import/export comparison are a page of
        # their own (/economy gdp) rather than three fields nobody reading a
        # queue length asked for.
        #
        # The lifetime mint/burn totals are not here either. They are the
        # operator's view of the same economy and are on the Ops dashboard
        # (web/queries.py), where the faucet/sink ratio they exist to show is
        # read alongside every other server's.
        embed.description = (
            f"Server wealth: {format_currency(circulating, currency_emoji)}\n"
            f"Mining slot progress: {format_currency(slot_progress(cfg), currency_emoji)}\n"
            f"Weekly GDP: {format_currency(week.gdp, currency_emoji)}"
        )

        # Every gem, including the ones at zero. A run of noughts is the
        # honest picture of a one-in-a-million drop rate, and omitting them
        # would make an empty field out of the commonest case.
        gem_cells = []
        for material_id in GEMSTONES:
            info = get_material_info(material_id)
            gem_cells.append(f"{info['emoji']} **{gems.get(material_id, 0):,}**")
        embed.add_field(
            name="Gemstones Mined (7d)",
            value=" \u00b7 ".join(gem_cells) + "\n(Not counted toward GDP)",
            inline=False,
        )

        queue_lines, queued_items, queued_jobs, queue_wait = await self._queue_lines(
            interaction.guild_id, cfg
        )
        add_multi_field(
            embed,
            queue_field_name(queued_items, queued_jobs, queue_wait),
            queue_lines,
            empty_text="Nothing queued.",
        )

        if job is not None:
            claims = await self.db.fetchone(
                "SELECT COALESCE(SUM(claims_paid), 0) AS completions "
                "FROM daily_job_progress WHERE guild_id = ? AND job_date = ?",
                (interaction.guild_id, job["job_date"]),
            )
            completions = claims["completions"] if claims else 0
            info = get_material_info(job["material_id"])
            times = "time" if completions == 1 else "times"
            embed.add_field(
                name="Today's Job Board",
                value=(
                    f"Sell {info['emoji']} **{job['quantity']:,} {material_name(info, job['quantity'])}** \u00b7 pays "
                    f"{format_currency(job['reward'], currency_emoji)} per completion\n"
                    f"Finished **{completions:,}** {times} today \u00b7 a new job is posted "
                    f"{format_relative_timestamp(hours_until_reset())}."
                ),
                inline=False,
            )

        await respond(interaction, self.db, embed=embed)

    @economy_group.command(
        name="gdp",
        description="What this server produced: value added by stage, over 24h and 7d",
    )
    async def economy_gdp(self, interaction: discord.Interaction):
        """Everything GDP, on a page of its own.

        Read-only in full - unlike /economy status it has no job board to post,
        so it writes nothing at all.

        Split off /economy status because the two answer different questions.
        The status page needs one number for what the server produced; this is
        where that number is taken apart - both windows, which stage of
        production added the value, and whether the machines here ran on more
        than this server dug up. Somebody reading a queue length does not want
        three fields of national accounts, and somebody asking why the figure
        moved wants all of them.
        """
        guild_name = interaction.guild.name if interaction.guild else "This Server"

        cfg = await self.db.fetchone(
            "SELECT currency_name, currency_emoji FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        currency_emoji = cfg["currency_emoji"] if cfg else None

        day = await window_totals(
            self.db, interaction.guild_id, window_cutoff(GDP_DAY_HOURS)
        )
        week_cutoff = window_cutoff(GDP_WEEK_HOURS)
        week = await window_totals(self.db, interaction.guild_id, week_cutoff)
        first_seen = await tracked_since(self.db, interaction.guild_id)

        embed = make_embed("Server GDP", MARKET_COLOR)
        # Names the subcommand, not just the command: /economy status and
        # /market status are already three yellow embeds apart from each other
        # on the author line alone (docs/stylization.md), and this is a fourth.
        embed.set_author(name=f"\U0001F4CA Economy \u2022 GDP \u2022 {guild_name}")

        # Value added, counted once per stage: what a thing sold for minus what
        # was consumed making it (docs/market.md section 5). Said here rather
        # than on the status page because this is the page somebody opens to
        # find out what the figure means.
        embed.description = (
            f"What this server **produced**, valued at market prices - each stage "
            f"counted once, at what it added.\n\n"
            f"{self._gdp_windows_value(day, week, first_seen, currency_emoji)}"
        )

        breakdown = self._value_breakdown_lines(week, currency_emoji)
        if breakdown:
            add_multi_field(embed, "Where The Value Came From (7d)", breakdown)

        await respond(interaction, self.db, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
