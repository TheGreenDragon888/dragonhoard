"""
utils/mining_affinity.py

A player's mining affinity: which gemstone every other gem they mine arrives as.

The balance data and the conversion arithmetic live in data/materials.py
(MINING_AFFINITIES, apply_mining_affinity); this is the database half - who has
unlocked it, what they have chosen, what they are part-way toward, and the
once-a-day limit on changing it.

It deliberately mirrors utils/mining_focus.py, because an affinity IS a focus
one tier up: same single carry, same row-is-the-unlock, same daily limit. Three
rules differ, and all three come from the same fact - a fraction of a gem is
worth thousands of times what a fraction of an ore is.

  THE CARRY IS CONVERTED ON A CHANGE, NEVER RESET. A focus resets its carry
  because the most that can destroy is a fraction of one iron ore. The most
  this can destroy is a fraction of a diamond - up to 89 rubies of mining - so
  it is converted to the new target at the RARITY ratio instead. Rarity, not
  the bonused rate: re-bonusing already-earned value on every change would let
  a player pump a fraction of a diamond into a whole one by switching back and
  forth, which is a printer rather than a feature.

  A CHANGE PAYS OUT WHAT IT CAN. Converting 0.99 of a diamond into rubies
  leaves 89.1 rubies owed, and a carry is supposed to be a fraction. So the
  whole units fall out at that moment and only the remainder is kept, which is
  also the only time a player can be handed gems by a command that isn't
  /collect.

  IT IS SURFACED, NOT INTERNAL. The other two carries are implementation
  detail. This one is the player's progress toward the rarest thing in the
  game, so affinity_progress exists to render it and three separate places in
  cogs/mining.py show it.

Conversion happens at COLLECTION, after the focus and the efficiency, for the
reason utils/mining_focus.py gives: a drill banks real materials drawn from its
server's pool, which is what keeps the pool's gemstone guarantee honest, and
what a player has committed to only decides what those materials become on the
way into the inventory.
"""
import logging

from database.db import _Executor
from data.materials import (
    DEFAULT_MINING_AFFINITY,
    GEMSTONES,
    MINING_AFFINITIES,
    RAW_MATERIALS,
    accrue,
    affinity_conversion_rate,
    apply_mining_affinity,
    focus_conversion_rate,
)
from utils.db_helpers import adjust_user_quantity

log = logging.getLogger("dragonhoard")


async def get_affinity(db: _Executor, user_id: int) -> tuple[str, float, str, bool]:
    """This player's (affinity_id, carry, last_changed, unlocked).

    Someone who has never unlocked it reads as the default with no carry rather
    than as an error - every collection path calls this, and "has not bought
    the feature" is the overwhelmingly common case. An unrecognised affinity_id
    falls back the same way, so removing one from MINING_AFFINITIES can never
    strand the players who had chosen it.
    """
    row = await db.fetchone(
        "SELECT affinity_id, carry, last_changed FROM user_mining_affinity WHERE user_id = ?",
        (user_id,),
    )
    if row is None:
        return DEFAULT_MINING_AFFINITY, 0.0, "", False
    affinity_id = row["affinity_id"]
    if affinity_id not in MINING_AFFINITIES:
        log.warning(
            "User %s has unknown mining affinity %r - using the default.", user_id, affinity_id
        )
        affinity_id = DEFAULT_MINING_AFFINITY
    return affinity_id, row["carry"], row["last_changed"], True


async def convert_gems(tx: _Executor, user_id: int, breakdown: dict[str, int]) -> dict[str, int]:
    """Applies this player's affinity to a haul on its way out of a drill, and
    banks whatever fraction of a gem the conversion left owing.

    Called from the same places convert_haul and boost_haul are, immediately
    after them, so an affinity can't be dodged by choosing a different way to
    pick materials up. It runs LAST because the other two only touch ore and
    this only touches gems - the order is arbitrary between them today, and
    stating one keeps it arbitrary rather than accidental.

    Persisting the carry is what makes the feature honest in both directions:
    45 rubies collected one at a time have to come to the same diamond that 45
    collected at once do.
    """
    affinity_id, carry, _, unlocked = await get_affinity(tx, user_id)
    if not unlocked or not breakdown:
        return breakdown

    converted, new_carry = apply_mining_affinity(affinity_id, breakdown, carry)
    if new_carry != carry:
        await tx.execute(
            "UPDATE user_mining_affinity SET carry = ? WHERE user_id = ?", (new_carry, user_id)
        )
    return converted


async def set_affinity(
    tx: _Executor, user_id: int, affinity_id: str, today: str
) -> dict[str, int]:
    """Records a chosen affinity, carrying the player's accrued progress over
    to it, and returns whatever whole gems that conversion paid out (already
    credited).

    The first call is the unlock - the caller having taken
    MINING_AFFINITY_UNLOCK_COST - and every later one is a change; there is
    nothing different about them here except that an unlock has no prior carry
    to move.

    Converting at focus_conversion_rate rather than affinity_conversion_rate is
    load-bearing, not an oversight: see the module docstring. Switching to
    "none" banks the progress as the gem it was accrued in, because a player
    turning the feature off should not forfeit what they had already earned.
    """
    row = await tx.fetchone(
        "SELECT affinity_id, carry FROM user_mining_affinity WHERE user_id = ?", (user_id,)
    )
    carry = 0.0
    paid: dict[str, int] = {}
    if row is not None and row["carry"] > 0:
        old_target = MINING_AFFINITIES.get(row["affinity_id"], {}).get("primary")
        # Turning the feature off leaves nothing to accrue toward, so the
        # progress is cashed out in the commonest gem instead of being held in
        # units of a target that no longer exists. Converting down always lands
        # on whole numbers of the base gem, so what that drops is a fraction of
        # one RUBY rather than a fraction of the target - the difference
        # between forfeiting 0.1 of a ruby and forfeiting 89 rubies' worth of a
        # diamond, which is the whole reason this isn't the reset a focus does.
        new_target = MINING_AFFINITIES[affinity_id]["primary"] or GEMSTONES[0]
        if old_target is not None:
            carry = row["carry"] * focus_conversion_rate(old_target, new_target)
            whole, carry = accrue(0.0, carry)
            if whole:
                paid[new_target] = whole
        if MINING_AFFINITIES[affinity_id]["primary"] is None:
            carry = 0.0

    for material_id, quantity in paid.items():
        await adjust_user_quantity(tx, user_id, material_id, quantity)

    await tx.execute(
        "INSERT INTO user_mining_affinity (user_id, affinity_id, carry, last_changed) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "affinity_id = excluded.affinity_id, carry = excluded.carry, "
        "last_changed = excluded.last_changed",
        (user_id, affinity_id, carry, today),
    )
    return paid


def affinity_label(affinity_id: str) -> str:
    affinity = MINING_AFFINITIES.get(affinity_id) or MINING_AFFINITIES[DEFAULT_MINING_AFFINITY]
    return f"{affinity['emoji']} {affinity['name']}"


def affinity_progress(affinity_id: str, carry: float) -> str | None:
    """How far along the next gem is, as a percentage, or None when there is
    nothing to say.

    A PERCENTAGE rather than a count of some source gem, which is what this
    used to show. The carry is one number no matter what fed it, but a count
    has to name a gem to count in - and naming one misreports every haul that
    included another. A player who mined 38 rubies and 3 obsidian under a
    diamond affinity was told "23 of 45 Rubies", which is a true rendering of
    0.511 of a diamond and still reads as though the obsidian did nothing; it
    in fact did 0.667 of that diamond against the rubies' 0.844. The ambiguity
    was in the unit, so the unit is gone.

    Floored, never rounded: 0.999 of a diamond is 99%, because a line that
    reads 100% is a line claiming a gem the carry has not earned.

    A ruby affinity never reports anything, and the guard for that is
    structural rather than a named special case: every rate INTO a ruby makes
    at least one whole ruby (an obsidian is 20, a diamond 180), so there is
    never a fraction owing and a percentage would be reporting on a carry that
    cannot exist. Any future gem commoner than everything that converts into it
    inherits the same silence.

    The floor is the second guard, and it earns its place on the other two:
    it swallows the 0% a float remainder would otherwise print, and a line
    reading 0% is worth no more than no line at all.
    """
    target = MINING_AFFINITIES.get(affinity_id, {}).get("primary")
    if target is None or carry <= 0:
        return None
    if all(
        affinity_conversion_rate(source, target) >= 1
        for source in GEMSTONES
        if source != target
    ):
        return None
    percent = int(carry * 100)
    if percent < 1:
        return None
    info = RAW_MATERIALS[target]
    return f"**{percent}%** toward your next {info['emoji']} {info['name']}"
