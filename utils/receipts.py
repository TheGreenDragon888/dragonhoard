"""
utils/receipts.py

Builds the receipt embed shown when a user queues a job at one of the server's
machines. Because both fees and input materials are taken up front at queue
time (see cogs/furnace.py), the user has already been charged by the time they
see a response - so the response has to account for every item and coin that left
their inventory, and what they have left afterwards.

Remaining amounts are passed in by the caller rather than re-read from the
database: the queue commands already read every input quantity to validate
affordability, so the post-deduction figure is just (pre-read - deducted) and
costs no extra queries.
"""
import discord

from data.materials import get_material_info, material_name
from utils.embeds import make_embed, add_multi_field, footer_with
from utils.formatting import (
    format_currency,
    format_relative_timestamp,
    format_price,
    format_receipt_price,
    DEFAULT_CURRENCY_EMOJI,
)


# The word after the figure in a receipt line's parentheses: what the player
# has now. One word per direction, everywhere (docs/stylization.md, Receipts):
# "remaining" after something was taken, "total" after something was gained.
# 1.4 had grown four ("remaining", "balance", "held", "total") for two meanings.
SPENT = "remaining"
GAINED = "total"


def _material_line(material_id: str, amount: int, total_after: int, label: str = SPENT) -> str:
    """One material row: how much moved, and what that material is at now.

    label is SPENT for the queue receipts below, where the material is always
    consumed, and GAINED for a line where the player's stock went up.

    Thousands separators because the blast furnace consumes in the thousands
    (2,000 iron ore for one batch of steel), and an unseparated 540000 in a
    receipt is a number the reader has to count digits on."""
    info = get_material_info(material_id)
    emoji = info["emoji"] if info else "❓"
    name = material_name(info, amount) if info else material_id
    return f"{emoji} **{amount:,} {name}** ({total_after:,} {label})"


def material_line(material_id: str, amount: int, total_after: int, *, gained: bool) -> str:
    """A receipt's material row, with the one word for its direction."""
    return _material_line(material_id, amount, total_after, GAINED if gained else SPENT)


def currency_line(
    amount: float, balance_after: float, currency_emoji: str | None, *, gained: bool,
    after_label: str | None = None,
) -> str:
    """A receipt's currency row, in the same shape as a material row: emoji,
    the bolded amount that moved, and what is there now in parentheses.

    Rounded up when the amount was taken and down when it was paid, so a
    receipt never makes a charge look smaller or a payment look bigger than it
    was. `after_label` replaces the direction word when the figure in brackets
    is not the player's own balance (the treasury, on a Mayor's receipt).
    """
    word = after_label or (GAINED if gained else SPENT)
    return (
        f"{currency_emoji or DEFAULT_CURRENCY_EMOJI} "
        f"**{format_receipt_price(amount, round_up=not gained)}** "
        f"({format_price(balance_after)} {word})"
    )


def build_action_receipt(
    title: str,
    color: discord.Color,
    summary: str,
    fields: list[tuple[str, str]],
    *,
    footer_note: str | None = None,
) -> discord.Embed:
    """A receipt for anything that is not a machine job: a one-line summary of
    what happened, then one field per thing that moved or changed.

    The shape build_receipt_embed and build_market_receipt_embed already give
    production and trade, for everything else that moves goods or money -
    listings, orders, cancellations, the Mayor's projects, bonds - which had
    each been a paragraph of prose since 1.4 (docs/stylization.md, Receipts).
    `footer_note` is where a pointer to the next command goes, via
    footer_with, so it does not cost a line of the embed.
    """
    embed = make_embed(title, color, description=summary)
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)
    if footer_note:
        embed.set_footer(text=footer_with(footer_note))
    return embed


def build_receipt_embed(
    *,
    title: str,
    color: discord.Color,
    action: str,
    product_id: str,
    quantity: int,
    consumed: list[tuple[str, int, int]],
    fuel: tuple[str, int, int] | None = None,
    fuel_label: str = "Furnace Fuel",
    fee_total: float,
    balance_after: float,
    currency_emoji: str,
    product_label: tuple[str, str] | None = None,
    eta_hours: float | None = None,
) -> discord.Embed:
    """Assembles the queue receipt.

    consumed and fuel are (material_id, amount_consumed, remaining_after)
    tuples. fuel is kept separate from consumed so the furnace's flat
    per-item coal burn is visible as its own cost even when the recipe
    already consumes coal of its own (e.g. steel), which would otherwise
    hide it inside a single combined coal total. fuel_label names that field,
    because the blast furnace burns the same fuel under its own name.

    product_label overrides the (emoji, name) that product_id would look up.
    A drill level-up is queued as a factory job whose target is a sentinel
    rather than a material, so it has no entry to look up and would otherwise
    render as "❓ drill_upgrade".

    eta_hours is how long the whole job takes to come out the far end of the
    machine, queue included - the receipt is where a player finds out that the
    thing they just paid for lands tomorrow rather than in five minutes. It
    reads as the second sentence of the description rather than as a field at
    the bottom, because "when do I get it" is the question the receipt is
    answering, not a footnote to the cost breakdown. The time itself is one of
    Discord's relative timestamps, so it keeps counting down after the message
    is sent.
    """
    if product_label is not None:
        product_emoji, product_name = product_label
    else:
        product = get_material_info(product_id)
        product_emoji = product["emoji"] if product else "❓"
        product_name = material_name(product, quantity) if product else product_id

    description = f"Queued {product_emoji} **{quantity:,} {product_name}** for {action}."
    if eta_hours is not None:
        # The LAST item of the job, not the first - this is answering "when do I
        # have all of this", which is what was just paid for.
        description += f" It will be ready {format_relative_timestamp(eta_hours)}."

    embed = make_embed(title, color, description=description)

    add_multi_field(embed, "Consumed", [_material_line(*entry) for entry in consumed])

    if fuel is not None:
        add_multi_field(embed, fuel_label, [_material_line(*fuel)])

    if fee_total > 0:
        # Laid out like a consumed-material line: emoji, then the bolded
        # amount taken, then what's left in parentheses. That bold sitting
        # between the emoji and the number is why this composes format_price
        # by hand instead of calling format_currency. The remainder omits the
        # emoji because the one leading the line already established the unit,
        # just as a material's remainder doesn't repeat its emoji. round_up on
        # the charge so the receipt never understates what was taken; the
        # remainder floors for the same reason.
        embed.add_field(
            name="Fee Paid",
            value=(
                f"{currency_emoji or DEFAULT_CURRENCY_EMOJI} "
                f"**{format_receipt_price(fee_total, round_up=True)}** "
                f"({format_price(balance_after)} remaining)"
            ),
            inline=False,
        )
    else:
        embed.add_field(name="Fee Paid", value="Free", inline=False)

    return embed


def build_market_receipt_embed(
    *,
    title: str,
    color: discord.Color,
    description: str,
    material_field: str,
    material_id: str,
    quantity: int,
    material_remaining: int,
    material_gained: bool,
    currency_field: str,
    currency_amount: float,
    balance_after: float,
    currency_gained: bool,
    currency_emoji: str | None,
    round_up_currency: bool,
) -> discord.Embed:
    """Assembles a /market sell or /market buy receipt.

    A trade has no fee or fuel of its own - it's just a material moving one
    way and currency moving the other - but it's laid out like
    build_receipt_embed's queue receipts anyway: a description sentence, then
    one field per side of the trade. material_field/currency_field name those
    two sides ("Sold"/"Received" for a sale, "Bought"/"Spent" for a
    purchase), and each line reuses _material_line's and Fee Paid's exact
    shape - emoji, bolded amount, remainder in parentheses - so a trade
    reads the same way a production job's receipt does.

    Exactly one of material_gained/currency_gained is True per call - a trade
    always gives up one side and gains the other. SPENT ("remaining") only fits
    the side given up; the side gained just went up, so it takes GAINED
    ("total") instead of mislabeling a total that just grew.

    round_up_currency mirrors Fee Paid's round_up=True: round up when the
    amount is being taken from the user (a purchase must never look cheaper
    than it was) and down when it's being paid to them (a sale must never
    look more generous than it was).
    """
    embed = make_embed(title, color, description=description)

    embed.add_field(
        name=material_field,
        value=material_line(material_id, quantity, material_remaining, gained=material_gained),
        inline=False,
    )
    embed.add_field(
        name=currency_field,
        value=(
            f"{currency_emoji or DEFAULT_CURRENCY_EMOJI} "
            f"**{format_receipt_price(currency_amount, round_up=round_up_currency)}** "
            f"({format_price(balance_after)} {GAINED if currency_gained else SPENT})"
        ),
        inline=False,
    )

    return embed


def fill_lines(fills, currency_emoji: str | None, preposition: str) -> list[str]:
    """One line per counterparty on a trade that spanned more than the server,
    for the receipt's description.

    Returns EMPTY for a trade that cleared entirely against the server, which
    is what keeps every pre-1.4 receipt looking exactly as it did: the
    description sentence already says "to the server for X", and a single line
    repeating it underneath would be noise on the overwhelming majority of
    trades.

    `preposition` is "from" on a purchase and "to" on a sale - the same list
    describes both directions, and only this word differs.

    Players are named by mention. Embeds never fire a notification, and the
    client resolves a raw mention whether or not that member is cached - the
    same reasoning utils/embeds.py: job_owner_label is built on.
    """
    from utils.market_book import SERVER

    if all(fill.source is SERVER for fill in fills):
        return []
    lines = []
    for fill in fills:
        who = "the server" if fill.source is SERVER else f"<@{fill.counterparty}>"
        lines.append(
            f"· **{fill.quantity:,}** {preposition} {who} at "
            f"{format_currency(fill.total / fill.quantity, currency_emoji)} each"
        )
    return lines
