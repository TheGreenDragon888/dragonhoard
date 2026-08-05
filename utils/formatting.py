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


def utc_today() -> str:
    """Today's date in UTC, ISO formatted.

    Used by the mining pool top-up, which rolls over at UTC midnight. The job
    board runs on a different clock (utils/job_board.py: JOB_BOARD_TIMEZONE) -
    the two shared this function until the board moved to Arizona time, so
    don't assume a change here reaches both.

    Stored as text and compared as text, which is why the ISO format matters:
    it sorts and compares chronologically as a plain string."""
    return datetime.now(timezone.utc).date().isoformat()


def format_price(amount: float, round_up: bool = False) -> str:
    """The bare numeric text (no currency emoji) for a price, rounded to the
    nearest cent (2 decimals) for display - DOWN by default, or UP if
    round_up is set (e.g. when quoting a cost the user is about to be
    charged, so the displayed price is never less than what's taken). This
    only affects what's shown - the underlying balance keeps its full
    floating-point precision in the database. Nonzero amounts that would
    display as 0.00 (market prices are often worth fractions of a cent)
    extend to 4 decimals instead. The 1e-9 nudge guards against float
    imprecision landing an exact cent just on the wrong side of the
    floor/ceiling (e.g. 1.10 * 100 == 109.99999999999999), and flips sign
    with the rounding direction so it never over-corrects a value that's
    already exact."""
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
    digit slot per tier above it - 0.000K / 00.00K / 000.0K / 0.000M / ..."""
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
