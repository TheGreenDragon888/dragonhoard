"""
utils/formatting.py

Shared helpers so every command displays currency, and how long a machine will
take, the same way.
"""
import math
from datetime import datetime, timedelta, timezone

DEFAULT_CURRENCY_EMOJI = "💰"

# (threshold, suffix) pairs for format_compact_price, ascending order.
_COMPACT_TIERS = [
    (1, ""),
    (1_000, "K"),
    (1_000_000, "M"),
    (1_000_000_000, "B"),
    (1_000_000_000_000, "T"),
]


def plural(word: str, count: int | None = None) -> str:
    """The plural of one of the units the bot counts in, or the singular when
    `count` is exactly 1.

    Exists because "batch" is the one unit in the game that does not simply
    take an "s", and it was reaching players as "batchs" in three places at
    once - /blast status's queue heading, the queue limit under it, and the
    rejection when that limit is hit - all three of which take `unit` as an
    argument and pluralised it by concatenation. Fixing it at each call site
    would leave the next machine that counts in something other than items to
    rediscover it.

    Deliberately a sibilant rule rather than a dictionary: it covers "batch"
    and every other -s/-x/-z/-ch/-sh unit a machine might plausibly count in,
    and leaves "item" alone.
    """
    if count == 1:
        return word
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return f"{word}es"
    return f"{word}s"


def format_price(amount: float, round_up: bool = False) -> str:
    """The bare numeric text (no currency emoji) for a price, rounded to the
    nearest cent (2 decimals) for display - DOWN by default, or UP if
    round_up is set (e.g. when quoting a cost the user is about to be
    charged, so the displayed price is never less than what's taken). This
    only affects what's shown - the underlying balance keeps its full
    floating-point precision in the database. Nonzero amounts that would
    display as 0.00 extend to 4 decimals instead: market prices are whole
    cents as of 1.3, but a Treasurer's x0.25 multiplier puts the furnace's
    fee at a quarter of a cent (utils/government.py: FEE_MULTIPLIERS), and a
    sub-cent one shown as 0.00 would read as free. The 1e-9 nudge
    guards against float imprecision landing an exact cent just on the wrong
    side of the floor/ceiling (0.29 * 100 == 28.999999999999996, which would
    otherwise floor to 0.28), and flips sign with the rounding direction so
    it never over-corrects a value that's already exact."""
    if round_up:
        rounded_cents = math.ceil(amount * 100 - 1e-9) / 100
    else:
        rounded_cents = math.floor(amount * 100 + 1e-9) / 100
    if rounded_cents == 0 and amount != 0:
        return f"{amount:,.4f}"
    return f"{rounded_cents:,.2f}"


def format_currency(amount: float, emoji: str | None = None, round_up: bool = False) -> str:
    """The server's currency emoji preceding format_price's value."""
    return f"{emoji or DEFAULT_CURRENCY_EMOJI} {format_price(amount, round_up)}"


# The most decimals a machine's per-unit fee can have: its default is whole
# cents and the Treasurer's multipliers have at most three decimals (x0.625,
# utils/government.py: FEE_MULTIPLIERS). tests/test_formatting.py checks every
# fee on the ladder against it.
EXACT_PRICE_DECIMALS = 5


def format_exact_price(amount: float) -> str:
    """A per-unit fee in full, for the "Fee" lines that quote what a machine
    charges per item. format_price floors to the cent, which is right for a
    total but would show a furnace at x1.25 (1.25 cents an item) as 0.01 -
    less than it charges. Shows at least two decimals, and past the cent only
    the digits the fee actually has."""
    whole, _, fraction = f"{amount:,.{EXACT_PRICE_DECIMALS}f}".partition(".")
    return f"{whole}.{fraction.rstrip('0').ljust(2, '0')}"


def format_exact_currency(amount: float, emoji: str | None = None) -> str:
    """The server's currency emoji preceding format_exact_price's value."""
    return f"{emoji or DEFAULT_CURRENCY_EMOJI} {format_exact_price(amount)}"


# Below this, a receipt shows the fraction of a cent it moved.
RECEIPT_FINE_BELOW = 0.10
RECEIPT_FINE_DECIMALS = 4


def format_receipt_price(amount: float, round_up: bool = False) -> str:
    """The amount a receipt says was paid or received. Under ten cents it
    shows up to four decimals when there is something past the cent to show:
    seven items at a furnace on x0.625 take 0.04375, which reads 0.0438
    rather than 0.05. Trailing zeros past the cent are trimmed, so a
    whole-cent amount still reads 0.07. From ten cents up it is format_price.
    Rounds in the same direction format_price would, and at the fourth
    decimal: up for money taken, down for money paid out."""
    scale = 10 ** RECEIPT_FINE_DECIMALS
    if round_up:
        units = math.ceil(amount * scale - 1e-9)
    else:
        units = math.floor(amount * scale + 1e-9)
    cent = scale // 100
    if units % cent == 0 or units >= RECEIPT_FINE_BELOW * scale:
        return format_price(amount, round_up)
    return f"{units / scale:.{RECEIPT_FINE_DECIMALS}f}".rstrip("0")


def format_duration(hours: float) -> str:
    """How long something takes, in the largest two units that fit: "45m",
    "2h 15m", "3d 4h".

    Always rounds the minutes UP, for the same reason format_price rounds a
    charge up: a machine that says 2h 15m and delivers at 2h 16m has told the
    truth; one that says 2h 14m has not. Rounding up also means any real wait,
    however short, reads as at least "1m" - only a wait of nothing at all gets
    "under a minute", which is what a press with enough banked progress to
    finish a job on its next tick is actually looking at."""
    if hours <= 0:
        return "under a minute"

    total_minutes = math.ceil(hours * 60 - 1e-9)
    if total_minutes < 60:
        return f"{total_minutes}m"

    total_hours, minutes = divmod(total_minutes, 60)
    if total_hours < 24:
        return f"{total_hours}h {minutes}m" if minutes else f"{total_hours}h"

    days, leftover_hours = divmod(total_hours, 24)
    return f"{days}d {leftover_hours}h" if leftover_hours else f"{days}d"


def format_rate(value: float, unit: str | None = None) -> str:
    """A machine's speed for display, now that speed moves between levels
    (data/materials.py: effective_level): one decimal place below ten, whole
    numbers from ten up, and no trailing ".0" - so 7.5, 5 and 12. With `unit`,
    the unit follows it, singular only when what is printed is exactly "1" -
    a speed of 1.05 reads "1 item", not "1 items".

    Truncated rather than rounded, so the figure shown is never one the machine
    hasn't reached yet: a level 2 furnace a cent short of level 3 runs just
    under 15 an hour, which rounding would print as 15 - level 3's speed. The
    1e-9 is the same nudge format_price and max_affordable use, so a speed that
    lands a hair under a whole tenth on float arithmetic isn't cut down past
    it."""
    if value < 10:
        text = f"{math.floor(value * 10 + 1e-9) / 10:.1f}"
        text = text[:-2] if text.endswith(".0") else text
    else:
        text = f"{math.floor(value + 1e-9):,}"
    if unit is None:
        return text
    return f"{text} {plural(unit, 1 if text == '1' else None)}"


def format_relative_timestamp(hours: float) -> str:
    """A point `hours` from now, as Discord's own relative timestamp - the
    client renders it "in 2 hours", "in 3 days", in the reader's language.

    Rendered by the reader's client rather than by us, which is the whole
    point: an embed is written once and then sits in the channel going stale,
    so a baked-in "2h 15m" is only true at the moment it's sent. This stays
    true, and counts down on its own, however long the message is scrolled back
    to.

    Only valid in an embed's description or a field's VALUE. Discord does not
    render timestamp markup in titles, field names or author lines - those need
    format_duration instead."""
    ready_at = datetime.now(timezone.utc) + timedelta(hours=max(0.0, hours))
    return f"<t:{int(ready_at.timestamp())}:R>"


def _format_compact_tier(magnitude: float, threshold: float, suffix: str) -> str | None:
    """Formats magnitude at one specific tier, or returns None if rounding
    pushes it past 3 integer digits (e.g. 999.996 -> "1000.0") - the caller
    then retries at the next tier up, which has room."""
    scaled = magnitude / threshold
    digit_budget = 5 - len(suffix)
    for int_digits in (1, 2, 3):
        decimals = digit_budget - int_digits
        rounded = round(scaled, decimals)
        if rounded < 10 ** int_digits:
            return f"{rounded:.{decimals}f}{suffix}"
    return None


def format_compact_price(value: float) -> str:
    """A fixed 5-character-wide compact numeric string (digits plus an
    optional metric suffix, not counting the decimal point), so prices of
    any magnitude take up the same on-screen width and line up in embed
    text without a code block (which would stop custom material emoji from
    rendering): 0.0000 / 00.000 / 000.00 below 1000, then the same
    three-step digit-count pattern repeats with a K/M/B/T suffix eating one
    digit slot per tier above it - 0.000K / 00.00K / 000.0K / 0.000M / ...

    This is for PLAYER-SET prices only (/market list, /market order). The
    server's own prices need nothing but format_price: they are whole cents
    under a single currency unit, so they are four characters wide on their
    own and their column lines up for free - which is why this function was
    deleted in 1.3 when prices went static, and why the comment in
    cogs/economy.py's market_status still says so of that half of the embed.
    A player price is the case that argument does not cover. It runs from
    0.0001 (data/materials.py: PLAYER_PRICE_SCALE) to whatever somebody asks
    for a Diamond Drill, which is the "any magnitude, possibly a small
    fraction of a cent" condition this was written for in the first place.
    """
    magnitude = abs(value)
    tier_index = 0
    for i, (threshold, _) in enumerate(_COMPACT_TIERS):
        if magnitude >= threshold:
            tier_index = i
    for threshold, suffix in _COMPACT_TIERS[tier_index:]:
        text = _format_compact_tier(magnitude, threshold, suffix)
        if text is not None:
            return f"-{text}" if value < 0 else text
    # Larger than even the biggest suffix tier can express within 5 digits -
    # let the integer part grow past 3 digits rather than lose precision.
    threshold, suffix = _COMPACT_TIERS[-1]
    text = f"{magnitude / threshold:.0f}{suffix}"
    return f"-{text}" if value < 0 else text
