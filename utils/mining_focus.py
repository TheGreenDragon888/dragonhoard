"""
utils/mining_focus.py

A player's mining focus: which ore everything they mine arrives as.

The balance data and the conversion arithmetic live in data/materials.py
(MINING_FOCUSES, apply_mining_focus); this is the database half - who has
unlocked it, what they have chosen, and the once-a-day limit on changing it.

Three rules the rest of the code depends on:

  THE ROW IS THE UNLOCK. A player with no user_mining_focus row is on the
  default focus and has never paid the ruby. Inserting the row is what the
  payment buys, so there is no separate flag to drift out of sync with it.

  THE CARRY IS RESET ON EVERY CHANGE. It holds a fraction of the focus's
  primary ore still owed from rounding; carried across a change it would pay
  out a fraction of a copper as iron.

  CONVERSION HAPPENS AT COLLECTION, not at harvest. A drill mines and banks
  real materials drawn from its server's pool - that is what keeps the pool's
  gemstone guarantee honest - and the focus is applied when those materials are
  handed over. It follows that changing focus re-aims ore already sitting in a
  drill, which is harmless (the conversion rule is fixed, so there is nothing to
  time) and spares a player having to empty every drill before switching.
"""
import logging

from database.db import _Executor
from data.materials import (
    DEFAULT_MINING_FOCUS,
    MINING_FOCUSES,
    apply_mining_focus,
)

log = logging.getLogger("dragonhoard")


async def get_focus(db: _Executor, user_id: int) -> tuple[str, float, str, bool]:
    """This player's (focus_id, carry, last_changed, unlocked).

    Someone who has never unlocked it reads as the default focus with no carry,
    rather than as an error - every collection path calls this, and "has not
    bought the feature" is the overwhelmingly common case, not an exception.
    An unrecognised focus_id falls back the same way, so removing a focus from
    MINING_FOCUSES can never strand the players who had chosen it.
    """
    row = await db.fetchone(
        "SELECT focus_id, carry, last_changed FROM user_mining_focus WHERE user_id = ?",
        (user_id,),
    )
    if row is None:
        return DEFAULT_MINING_FOCUS, 0.0, "", False
    focus_id = row["focus_id"]
    if focus_id not in MINING_FOCUSES:
        log.warning("User %s has unknown mining focus %r - using the default.", user_id, focus_id)
        focus_id = DEFAULT_MINING_FOCUS
    return focus_id, row["carry"], row["last_changed"], True


async def convert_haul(tx: _Executor, user_id: int, breakdown: dict[str, int]) -> dict[str, int]:
    """Applies this player's focus to a haul on its way out of a drill, and
    banks whatever fraction of an ore the conversion left owing.

    Every path that empties a drill goes through here - /collect, /mine remove,
    and the retraction that happens when the bot leaves a server - so a focus
    can't be dodged by choosing a different way to pick your materials up.

    Persisting the carry is the part that matters. Without it, converting in
    small batches would round the same fraction away (or up) every time: a coal
    focus turns one iron ore into 0.264 coal, so twenty separate one-item
    collections have to come to the same five coal a single twenty-item
    collection does, and only a stored remainder makes that true.
    """
    focus_id, carry, _, unlocked = await get_focus(tx, user_id)
    if not unlocked or not breakdown:
        return breakdown

    converted, new_carry = apply_mining_focus(focus_id, breakdown, carry)
    if new_carry != carry:
        await tx.execute(
            "UPDATE user_mining_focus SET carry = ? WHERE user_id = ?", (new_carry, user_id)
        )
    return converted


async def set_focus(tx: _Executor, user_id: int, focus_id: str, today: str) -> None:
    """Records a chosen focus, resetting the rounding carry and stamping the
    day so the once-a-day limit has something to check.

    Upsert rather than insert-or-update by hand: the first call is the unlock
    (the caller having already taken MINING_FOCUS_UNLOCK_COST) and every later
    one is a change, and there is nothing different about them here."""
    await tx.execute(
        "INSERT INTO user_mining_focus (user_id, focus_id, carry, last_changed) "
        "VALUES (?, ?, 0.0, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "focus_id = excluded.focus_id, carry = 0.0, last_changed = excluded.last_changed",
        (user_id, focus_id, today),
    )


def focus_label(focus_id: str) -> str:
    focus = MINING_FOCUSES.get(focus_id) or MINING_FOCUSES[DEFAULT_MINING_FOCUS]
    return f"{focus['emoji']} {focus['name']}"
