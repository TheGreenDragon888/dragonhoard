"""
utils/receipts.py

Builds the receipt embed shown when a user queues a furnace or factory job.
Because both fees and input materials are taken up front at queue time (see
cogs/furnace.py), the user has already been charged by the time they see a
response - so the response has to account for every item and coin that left
their inventory, and what they have left afterwards.

Remaining amounts are passed in by the caller rather than re-read from the
database: the queue commands already read every input quantity to validate
affordability, so the post-deduction figure is just (pre-read - deducted) and
costs no extra queries.
"""
import discord

from data.materials import get_material_info
from utils.embeds import make_embed, add_multi_field
from utils.formatting import format_relative_timestamp, format_price, DEFAULT_CURRENCY_EMOJI


def _material_line(material_id: str, amount: int, remaining: int) -> str:
    """One consumed-material row: what came out of the inventory, and what
    that material is down to now."""
    info = get_material_info(material_id)
    emoji = info["emoji"] if info else "❓"
    name = info["name"] if info else material_id
    return f"{emoji} **{amount} {name}** ({remaining} remaining)"


def build_receipt_embed(
    *,
    title: str,
    color: discord.Color,
    action: str,
    product_id: str,
    quantity: int,
    consumed: list[tuple[str, int, int]],
    fuel: tuple[str, int, int] | None = None,
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
    hide it inside a single combined coal total.

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
        product_name = product["name"] if product else product_id

    description = f"Queued {product_emoji} **{quantity} {product_name}** for {action}."
    if eta_hours is not None:
        # The LAST item of the job, not the first - this is answering "when do I
        # have all of this", which is what was just paid for.
        description += f" It will be ready {format_relative_timestamp(eta_hours)}."

    embed = make_embed(title, color, description=description)

    add_multi_field(embed, "Consumed", [_material_line(*entry) for entry in consumed])

    if fuel is not None:
        add_multi_field(embed, "Furnace Fuel", [_material_line(*fuel)])

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
                f"**{format_price(fee_total, round_up=True)}** "
                f"({format_price(balance_after)} remaining)"
            ),
            inline=False,
        )
    else:
        embed.add_field(name="Fee Paid", value="Free", inline=False)

    return embed
