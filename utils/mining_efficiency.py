"""
utils/mining_efficiency.py

A player's mining efficiency: which smelted material their haul is boosted and
re-proportioned for.

The balance data and the arithmetic live in data/materials.py
(MINING_EFFICIENCIES, apply_mining_efficiency); this is the database half - who
has unlocked it, what they have chosen, and the once-a-day limit on changing.

It deliberately mirrors utils/mining_focus.py, because the two features have
the same shape at this layer even though they are independent of each other.
Three rules the rest of the code depends on:

  THE ROW IS THE UNLOCK. A player with no user_mining_efficiency row has never
  paid the obsidian, so there is no separate flag to drift out of sync with it.

  THE CARRIES ARE CLEARED ON EVERY CHANGE, all of them. They hold fractions of
  specific materials still owed, and an Iron efficiency's part-owed coal is not
  something a Steel efficiency should pay out.

  IT APPLIES AT COLLECTION, after the focus, on the whole haul at once. The
  focus decides what the materials ARE; the efficiency then decides how many of
  them there are, so running them the other way round would boost ore the focus
  was about to convert away.
"""
import logging

from database.db import _Executor
from data.materials import (
    DEFAULT_MINING_EFFICIENCY,
    MINING_EFFICIENCIES,
    apply_mining_efficiency,
)

log = logging.getLogger("dragonhoard")


async def get_efficiency(db: _Executor, user_id: int) -> tuple[str, str, bool]:
    """This player's (efficiency_id, last_changed, unlocked).

    Someone who has never unlocked it reads as the default rather than as an
    error - every collection path calls this, and "has not bought the feature"
    is the common case. An unrecognised efficiency_id falls back the same way,
    so removing one from MINING_EFFICIENCIES can never strand the players who
    had chosen it.
    """
    row = await db.fetchone(
        "SELECT efficiency_id, last_changed FROM user_mining_efficiency WHERE user_id = ?",
        (user_id,),
    )
    if row is None:
        return DEFAULT_MINING_EFFICIENCY, "", False
    efficiency_id = row["efficiency_id"]
    if efficiency_id not in MINING_EFFICIENCIES:
        log.warning(
            "User %s has unknown mining efficiency %r - using the default.", user_id, efficiency_id
        )
        efficiency_id = DEFAULT_MINING_EFFICIENCY
    return efficiency_id, row["last_changed"], True


async def boost_haul(tx: _Executor, user_id: int, breakdown: dict[str, int]) -> dict[str, int]:
    """Applies this player's efficiency to a haul on its way out of a drill,
    and banks whatever fractions of a material the correction left owing.

    Called from the same places convert_haul is, immediately after it, so an
    efficiency can't be dodged by choosing a different way to pick materials up.

    Persisting the carries is what stops the correction being a money printer
    in one direction and a shredder in the other: converting in small batches
    would otherwise round the same fraction away (or up) every time.
    """
    efficiency_id, _, unlocked = await get_efficiency(tx, user_id)
    if not unlocked or not breakdown:
        return breakdown

    rows = await tx.fetchall(
        "SELECT material_id, carry FROM user_mining_efficiency_carry WHERE user_id = ?",
        (user_id,),
    )
    carries = {row["material_id"]: row["carry"] for row in rows}

    boosted, new_carries = apply_mining_efficiency(efficiency_id, breakdown, carries)

    for material_id, carry in new_carries.items():
        if carries.get(material_id) == carry:
            continue
        await tx.execute(
            "INSERT INTO user_mining_efficiency_carry (user_id, material_id, carry) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id, material_id) DO UPDATE "
            "SET carry = excluded.carry",
            (user_id, material_id, carry),
        )
    return boosted


async def set_efficiency(tx: _Executor, user_id: int, efficiency_id: str, today: str) -> None:
    """Records a chosen efficiency, clearing every rounding carry and stamping
    the day so the once-a-day limit has something to check.

    Upsert rather than insert-or-update by hand: the first call is the unlock
    (the caller having already taken MINING_EFFICIENCY_UNLOCK_COST) and every
    later one is a change, and there is nothing different about them here."""
    await tx.execute(
        "INSERT INTO user_mining_efficiency (user_id, efficiency_id, last_changed) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "efficiency_id = excluded.efficiency_id, last_changed = excluded.last_changed",
        (user_id, efficiency_id, today),
    )
    await tx.execute(
        "DELETE FROM user_mining_efficiency_carry WHERE user_id = ?", (user_id,)
    )


def efficiency_label(efficiency_id: str) -> str:
    efficiency = (
        MINING_EFFICIENCIES.get(efficiency_id) or MINING_EFFICIENCIES[DEFAULT_MINING_EFFICIENCY]
    )
    return f"{efficiency['emoji']} {efficiency['name']}"
