"""
utils/formatting.py

Shared helpers so every command displays currency, and how long a machine will
take, the same way.
"""
import math
from datetime import datetime, timedelta, timezone

DEFAULT_CURRENCY_EMOJI = "💰"


def format_price(amount: float, round_up: bool = False) -> str:
    """The bare numeric text (no currency emoji) for a price, rounded to the
    nearest cent (2 decimals) for display - DOWN by default, or UP if
    round_up is set (e.g. when quoting a cost the user is about to be
    charged, so the displayed price is never less than what's taken). This
    only affects what's shown - the underlying balance keeps its full
    floating-point precision in the database. Nonzero amounts that would
    display as 0.00 extend to 4 decimals instead: market prices are whole
    cents as of 1.3, but a server can set any fee it likes with /setup fee,
    and a sub-cent one shown as 0.00 would read as free. The 1e-9 nudge
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
