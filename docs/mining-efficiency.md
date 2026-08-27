Mining Efficiency
=================

Read docs/mining.txt (Mining Focus) first. Efficiency is the second half of the
same idea, but it is a SEPARATE feature: it does not require a focus, is not
gated behind one, and its options do not correspond to the focus options.
Focus decides which ore you dig up; efficiency decides how much of it you get
and in what proportion.

Unlocked with obsidian, once. Intended for the mid-to-late game - the stretch
where a player is grinding toward a diamond - which is why it is priced a gem
tier above the focus rather than beside it.


What it does
------------

A player commits to one SMELTED material: Iron, Copper or Steel. On every
collection, two things happen to the raw materials that recipe uses, in this
order:

  1. BOOST. Every raw material the recipe needs is multiplied by
     MINING_EFFICIENCY_BOOST (+100%).

  2. CORRECT. Up to MINING_EFFICIENCY_CORRECTION_CAP (20%) of whichever of
     those materials is in SURPLUS is converted into the one that is SHORT, at
     the rarity ratio a focus uses - stopping the moment the recipe's exact
     ratio is reached.

Materials the recipe does not use are untouched, as are all gemstones. A player
on an Iron efficiency still mines copper ore at exactly the normal rate.

The two steps are independent knobs. The boost sets the floor on what the
feature is worth; the correction decides how much of the haul is left over.
Neither can break the other, which is the reason for keeping them separate
rather than folding them into one multiplier.


Why there is a ratio to correct at all
--------------------------------------

FURNACE_COAL_COST_PER_UNIT is 1 - the furnace burns a coal per item smelted on
top of whatever the recipe lists. So the true cost of a unit is not what
SMELTED_MATERIALS says:

| Smelted | Recipe | + fuel | True cost | Ore : coal |
|---|---|---|---|---|
| Iron | 10 iron ore | 1 coal | 10 ore + 1 coal | 10 : 1 |
| Copper | 10 copper ore | 1 coal | 10 ore + 1 coal | 10 : 1 |
| Steel | 20 iron ore + 4 coal | 1 coal | 20 ore + 5 coal | 4 : 1 |

That fuel coal is load-bearing for the whole feature. Without it Iron and
Copper would need no coal at all and there would be no ratio to correct.


What it delivers
----------------

Units of the smelted material per 10,000 items mined, against the same focus
without efficiency and against a player who has neither feature:

| Efficiency | Focus | Units/10k | vs. focus | vs. neither | Consumed | Converted |
|---|---|---|---|---|---|---|
| Iron | Balance | 1360.1 | +140% | +140% | 93.5% | 20% (capped) |
| Iron | Iron & Coal | 2467.2 | +118% | +335% | 100% | 17.7% (exact) |
| Copper | Balance | 680.0 | +140% | +140% | 81.3% | 20% (capped) |
| Copper | Copper & Coal | 1246.7 | +120% | +340% | 92.3% | 20% (capped) |
| Steel | Balance | 582.6 | +106% | +106% | 100% | 2.8% (exact) |
| Steel | Iron & Coal | 839.2 | +180% | +196% | 93.9% | 20% (capped) |

The lowest gain over the same focus without efficiency is +105.6% (Steel on
Balance). That floor is the design target: every option, under every focus that
can supply it at all, at least doubles.

Steel on Iron & Coal is the headline at +180%, and it is not a coincidence.
Iron & Coal doubles a player's iron ore, but for steel the coal goes binding
immediately, so the focus alone delivers +5.8%. Steel efficiency is what lets
the focus's doubling actually land.


Why the correction is CAPPED
----------------------------

The cap is not a tuning nicety. It is what keeps Mining Focus a meaningful
choice, and removing it quietly destroys that feature.

Correcting all the way to the exact ratio every time gives 100% consumption in
every combination, which looks strictly better. It is not, because a
rarity-ratio conversion is even in mining effort - so a full correction erases
the difference between focuses, and Coal focus, which converts everything into
the densest ore, becomes a universal wildcard equal to the matched focus for
every recipe:

| Uncapped | Matched focus | Coal focus |
|---|---|---|
| Iron | 2467.2 | 2467.2 |
| Copper | 1429.7 | 1429.7 |
| Steel | 873.9 | 873.9 |

At a 20% cap that collapse does not happen: Iron on a Coal focus is 680.0
against Iron & Coal's 2467.2, so the focus that matches your efficiency is
still far and away the best one to be on.

Two consequences of stopping at the exact ratio rather than always converting a
flat percentage:

  - There is NO CLIFF. An earlier draft converted a fixed 20% and had a hard
    ceiling at 24.36%, past which Iron efficiency produced less Iron than no
    efficiency at all. Stopping at the exact ratio removes that entirely -
    raising the cap can never reduce output, it can only stop helping.
  - The cap BINDS in four of the six live combinations. Iron on Iron & Coal
    reaches the exact ratio at 17.68% and Steel on Balance at 2.8%; the rest
    hit 20% and stop short. So the cap is doing real work, not sitting unused.


Mismatched pairings
-------------------

Because efficiency is independent of focus, a player can pick a combination
that does not make sense. Those combinations are bad but not exploitable:

| | Units/10k | vs. neither feature |
|---|---|---|
| Iron on Copper & Coal | 226.7 | -60% |
| Copper on Iron & Coal | 113.3 | -60% |
| Steel on Copper & Coal | 113.3 | -60% |
| any efficiency on Coal focus | 340.0 - 680.0 | +20% |

They are worse than mining unfocused, and better than the zero a player would
get from that focus without efficiency - the efficiency is helping, the focus
is hurting. This is the same trap docs/mining.txt already documents for steel
under Copper & Coal, and it wants the same treatment: say so plainly in the
picker rather than forbidding the combination.


What it costs the economy
-------------------------

This is the largest raw-material injection of any design considered. Ore-only
market value per item mined runs 164.6% to 203.0% of a player with neither
feature across the six live combinations - lowest for Steel on Balance,
highest for Copper on Copper & Coal - since the boost doubles two materials
outright and the correction adds on top of that.

(These were 162.5% to 214.6% before 1.3 rounded the ore prices to whole cents,
which moved copper ore up relative to iron ore and narrowed the spread between
the combinations. tests/test_mining_efficiency.py pins the current figures.)

That is deliberate and it is what makes the feature worth an obsidian, but it
is a permanent, unbounded increase in what enters the market - and as of 1.3
the market pays a FLAT price rather than one that decays as its shelves fill
(docs/market.md section 3), so nothing damps it on the way in any more. It is
the one number here worth revisiting after beta.

For scale on the gate: a ruby is one per 11,111 items mined and an obsidian one
per 111,111, so unlocking efficiency costs ten times what unlocking a focus
does. The press route is 3,000 Copper.


Implementation notes
--------------------

- **Order.** Boost, then correct. The correction reads the ratio AFTER the
  boost - which is the same ratio, since the boost is proportional, but writing
  it in this order keeps the two knobs independent if the boost ever stops
  being uniform across a recipe's inputs.
- **Efficiency applies AFTER focus**, on the combined haul, for the same reason
  the focus applies at collection rather than at harvest.
- **Carries are per material.** The correction produces fractions on both
  sides, and which material each lands on depends on the player's focus, so a
  single carry column of the kind user_mining_focus has would pay a fraction of
  a coal out as iron ore. user_mining_efficiency_carry holds one carry per
  material instead. Keyed that way they are direction-agnostic and would
  survive an efficiency change correctly; they are cleared anyway as a clean
  slate on a paid action, which costs at most a fraction of one item.
- **Gemstones are never touched**, exactly as with a focus.
- **The blast furnace needs nothing.** Its recipes are derived at 100x
  (BLAST_FURNACE_RECIPES) and its fuel scales with them, so the ratios are
  identical and every figure above holds.
- **Coal focus plus any efficiency is nearly dead** - that stream has no iron
  or copper ore at all, so only the correction has anything to work with. Worth
  a word in the picker.


How these numbers were derived
------------------------------

All of it from RAW_MATERIALS drop chances, SMELTED_MATERIALS recipes and
FURNACE_COAL_COST_PER_UNIT, with no live data. For a focus stream s (expected
units per one item drawn from the pool) and a recipe's true cost need:

    boost:   a[k] = s[k] * (1 + BOOST)              for k in need
    correct: short/surplus by smaller a[k] / need[k]
             f_exact = (tgt*a[sur] - a[sh]) / (a[sur] * (rate + tgt))
                       where tgt = need[sh]/need[sur], rate = rarity(sur -> sh)
             f = min(CAP, f_exact); move a[sur]*f, receive that * rate
    units smelted = min(a[k] / need[k] for k in need)
    consumed      = units * sum(need) / sum(a[k] for k in need)

Per-10,000 figures are units x 10,000. tests/test_mining_efficiency.py pins
every figure in the tables above, including the uncapped-collapse comparison
and the +105.6% floor.
