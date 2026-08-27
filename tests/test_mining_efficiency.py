"""
Tests for Mining Efficiency: the obsidian-gated feature that doubles the raw
materials one smelted recipe needs and then trims their ratio toward what the
furnace actually charges.

Every figure in docs/mining-efficiency.md is pinned here, because the whole
design rests on numbers that are invisible at runtime - nothing in the game
ever displays "units per 10,000 items mined", so a retune that quietly halved
the feature's value would not show up anywhere else.

Three properties get the most attention, being the three that would each fail
silently:

  * THE FLOOR. Every live combination at least doubles its output against the
    same focus without efficiency. That promise is the reason the boost is
    +100% and is what the price of an obsidian is set against.
  * THE CAP IS LOAD-BEARING. Correcting all the way to the exact ratio would
    make Coal focus a universal wildcard equal to the matched focus for every
    recipe, which destroys Mining Focus as a choice. The cap is what prevents
    it, so the collapse is tested for directly rather than assumed.
  * THE CARRIES. Fractions land on two materials at once and which ones depends
    on the player's focus, so collecting in small batches has to come to
    exactly what collecting in one go does.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database.db import Database
from data.materials import (
    DEFAULT_MINING_EFFICIENCY,
    FURNACE_COAL_COST_PER_UNIT,
    MINING_EFFICIENCIES,
    MINING_EFFICIENCY_BOOST,
    MINING_EFFICIENCY_CORRECTION_CAP,
    MINING_EFFICIENCY_UNLOCK_COST,
    MINING_FOCUSES,
    ORES,
    RAW_MATERIALS,
    SMELTED_MATERIALS,
    apply_mining_efficiency,
    efficiency_correction,
    focus_conversion_rate,
    recipe_true_inputs,
)
from utils.db_helpers import ensure_user_row, get_user_quantity
from utils.mining_efficiency import boost_haul, get_efficiency, set_efficiency

USER = 909
PER = 10_000  # items drawn from the pool, the unit every published figure uses


def focus_stream(focus_id):
    """Expected units of each ore per one item drawn from the pool, after a
    focus has converted the haul. Mirrors apply_mining_focus in floats, since
    the published figures are expectations rather than a particular haul."""
    out = {ore: RAW_MATERIALS[ore]["drop_chance"] for ore in ORES}
    focus = MINING_FOCUSES[focus_id]
    if focus["primary"] is None:
        return out
    owed = 0.0
    for ore in list(out):
        if ore not in focus["keep"]:
            owed += out.pop(ore) * focus_conversion_rate(ore, focus["primary"])
    out[focus["primary"]] = out.get(focus["primary"], 0.0) + owed
    return out


def smelt_units(produces, amounts):
    needed = recipe_true_inputs(produces)
    return min(amounts.get(k, 0.0) / v for k, v in needed.items())


def with_efficiency(produces, focus_id):
    """The doc's arithmetic, driven by the real efficiency_correction: boost
    every input, then correct up to the cap. Returns amounts per PER items."""
    needed = recipe_true_inputs(produces)
    stream = focus_stream(focus_id)
    amounts = {k: stream.get(k, 0.0) * PER * (1 + MINING_EFFICIENCY_BOOST) for k in needed}
    short, surplus, fraction = efficiency_correction(amounts, needed)
    if fraction > 0:
        moved = amounts[surplus] * fraction
        amounts[surplus] -= moved
        amounts[short] += moved * focus_conversion_rate(surplus, short)
    return amounts


def units_with(produces, focus_id):
    return smelt_units(produces, with_efficiency(produces, focus_id))


def units_without(produces, focus_id):
    stream = focus_stream(focus_id)
    return smelt_units(produces, {k: v * PER for k, v in stream.items()})


class TrueRecipeCostTests(unittest.TestCase):
    """The fuel coal is the only reason Iron and Copper need coal at all, so
    every ratio in the feature is measured against recipe_true_inputs rather
    than the recipe as written."""

    def test_the_fuel_coal_is_added_to_every_recipe(self):
        for material_id in ("iron", "copper", "steel"):
            listed = SMELTED_MATERIALS[material_id]["inputs"].get("coal", 0)
            self.assertEqual(
                recipe_true_inputs(material_id)["coal"],
                listed + FURNACE_COAL_COST_PER_UNIT,
            )

    def test_iron_and_copper_would_need_no_coal_without_it(self):
        self.assertNotIn("coal", SMELTED_MATERIALS["iron"]["inputs"])
        self.assertNotIn("coal", SMELTED_MATERIALS["copper"]["inputs"])
        self.assertEqual(recipe_true_inputs("iron")["coal"], 1)

    def test_steel_wants_four_to_one_not_five_to_one(self):
        # docs/mining.txt and the MINING_FOCUSES comment both used to say 5:1,
        # which is the recipe read without the fuel coal.
        needed = recipe_true_inputs("steel")
        self.assertEqual(needed["iron_ore"] / needed["coal"], 4.0)


class PublishedFiguresTests(unittest.TestCase):
    """The table in docs/mining-efficiency.md, to one decimal place."""

    LIVE = {
        ("iron", "balanced"): 1360.1,
        ("iron", "iron"): 2467.2,
        ("copper", "balanced"): 680.0,
        ("copper", "copper"): 1246.7,
        ("steel", "balanced"): 582.6,
        ("steel", "iron"): 839.2,
    }
    MISMATCHED = {
        ("iron", "copper"): 226.7,
        ("iron", "coal"): 680.0,
        ("copper", "iron"): 113.3,
        ("copper", "coal"): 340.0,
        ("steel", "copper"): 113.3,
        ("steel", "coal"): 340.0,
    }

    def test_units_per_ten_thousand_mined(self):
        for (produces, focus_id), expected in {**self.LIVE, **self.MISMATCHED}.items():
            with self.subTest(produces=produces, focus=focus_id):
                self.assertAlmostEqual(units_with(produces, focus_id), expected, places=1)

    def test_every_live_combination_at_least_doubles(self):
        for produces, focus_id in self.LIVE:
            with self.subTest(produces=produces, focus=focus_id):
                gain = units_with(produces, focus_id) / units_without(produces, focus_id)
                self.assertGreaterEqual(gain, 2.0)

    def test_the_floor_is_steel_on_a_balance_focus(self):
        gains = {
            (p, f): units_with(p, f) / units_without(p, f) - 1 for p, f in self.LIVE
        }
        self.assertEqual(min(gains, key=gains.get), ("steel", "balanced"))
        self.assertAlmostEqual(min(gains.values()), 1.056, places=3)

    def test_steel_on_iron_focus_is_the_headline(self):
        # Iron & Coal doubles a player's iron ore but coal goes binding, so the
        # focus alone delivers 5.8%. This is what lets that doubling land.
        self.assertAlmostEqual(
            units_without("steel", "iron") / units_without("steel", "balanced") - 1,
            0.058, places=3,
        )
        self.assertAlmostEqual(
            units_with("steel", "iron") / units_without("steel", "iron") - 1,
            1.800, places=3,
        )

    def test_consumption_of_the_haul(self):
        expected = {
            ("iron", "balanced"): 93.5, ("iron", "iron"): 100.0,
            ("copper", "balanced"): 81.3, ("copper", "copper"): 92.3,
            ("steel", "balanced"): 100.0, ("steel", "iron"): 93.9,
        }
        for (produces, focus_id), pct in expected.items():
            with self.subTest(produces=produces, focus=focus_id):
                amounts = with_efficiency(produces, focus_id)
                needed = recipe_true_inputs(produces)
                used = smelt_units(produces, amounts) * sum(needed.values())
                self.assertAlmostEqual(used / sum(amounts.values()) * 100, pct, places=1)


class EconomicInjectionTests(unittest.TestCase):
    """What the feature puts into the market.

    This is the largest raw-material injection of any design considered and the
    one number docs/mining-efficiency.md flags for revisiting after beta, so it
    is pinned rather than left to be rediscovered. Ore only - gemstone expected
    value dwarfs the ores by roughly a hundred to one and would swamp the
    comparison entirely.
    """

    BASELINE = sum(
        RAW_MATERIALS[ore]["drop_chance"] * RAW_MATERIALS[ore]["market_price"]
        for ore in ORES
    )

    def _ore_value(self, produces, focus_id):
        stream = {k: v * PER for k, v in focus_stream(focus_id).items()}
        stream.update(with_efficiency(produces, focus_id))
        return sum(
            v * RAW_MATERIALS[k]["market_price"] for k, v in stream.items()
        ) / PER

    def test_published_injection_figures(self):
        expected = {
            ("iron", "balanced"): 167.2,
            ("iron", "iron"): 202.6,
            ("copper", "copper"): 203.0,
            ("copper", "balanced"): 167.2,
            ("steel", "balanced"): 164.6,
            ("steel", "iron"): 194.1,
        }
        for (produces, focus_id), pct in expected.items():
            with self.subTest(produces=produces, focus=focus_id):
                self.assertAlmostEqual(
                    self._ore_value(produces, focus_id) / self.BASELINE * 100, pct, places=1
                )

    def test_the_published_range_holds(self):
        values = [
            self._ore_value(p, f) / self.BASELINE * 100
            for p, f in PublishedFiguresTests.LIVE
        ]
        self.assertAlmostEqual(min(values), 164.6, places=1)
        self.assertAlmostEqual(max(values), 203.0, places=1)


class CorrectionCapTests(unittest.TestCase):
    """The cap is what keeps Mining Focus a meaningful choice."""

    def test_uncapped_makes_coal_focus_a_universal_wildcard(self):
        matched = {"iron": "iron", "copper": "copper", "steel": "iron"}
        with patch("data.materials.MINING_EFFICIENCY_CORRECTION_CAP", 1.0):
            for produces, focus_id in matched.items():
                with self.subTest(produces=produces):
                    self.assertAlmostEqual(
                        units_with(produces, "coal"),
                        units_with(produces, focus_id),
                        places=1,
                    )

    def test_the_cap_prevents_that_collapse(self):
        matched = {"iron": "iron", "copper": "copper", "steel": "iron"}
        for produces, focus_id in matched.items():
            with self.subTest(produces=produces):
                self.assertLess(
                    units_with(produces, "coal"),
                    units_with(produces, focus_id) / 2,
                )

    def test_raising_the_cap_never_reduces_output(self):
        # Stopping at the exact ratio is what removes the cliff an earlier
        # draft had at 24.36%, where converting a flat percentage started
        # draining the fuel coal the furnace needed.
        for produces, focus_id in (("iron", "iron"), ("steel", "iron"), ("copper", "copper")):
            previous = 0.0
            for cap in (0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.9):
                with patch("data.materials.MINING_EFFICIENCY_CORRECTION_CAP", cap):
                    current = units_with(produces, focus_id)
                self.assertGreaterEqual(current + 1e-6, previous, f"{produces}/{focus_id} @ {cap}")
                previous = current

    def test_the_cap_binds_in_four_of_the_six_live_combinations(self):
        reached_exact, capped = [], []
        for produces, focus_id in PublishedFiguresTests.LIVE:
            needed = recipe_true_inputs(produces)
            stream = focus_stream(focus_id)
            amounts = {k: stream.get(k, 0.0) * PER * (1 + MINING_EFFICIENCY_BOOST) for k in needed}
            _, _, fraction = efficiency_correction(amounts, needed)
            (capped if fraction >= MINING_EFFICIENCY_CORRECTION_CAP - 1e-9
             else reached_exact).append((produces, focus_id))
        self.assertEqual(len(capped), 4)
        self.assertCountEqual(reached_exact, [("iron", "iron"), ("steel", "balanced")])


class ApplyEfficiencyTests(unittest.TestCase):
    def test_the_default_changes_nothing(self):
        haul = {"iron_ore": 100, "coal": 20, "ruby": 1}
        out, carries = apply_mining_efficiency(DEFAULT_MINING_EFFICIENCY, haul)
        self.assertEqual(out, haul)
        self.assertEqual(carries, {})

    def test_gemstones_are_never_touched(self):
        haul = {"iron_ore": 1000, "coal": 200, "ruby": 3, "obsidian": 1, "diamond": 1}
        out, _ = apply_mining_efficiency("iron", haul)
        for gem in ("ruby", "obsidian", "diamond"):
            self.assertEqual(out[gem], haul[gem])

    def test_materials_outside_the_recipe_are_untouched(self):
        # An Iron efficiency leaves copper ore at exactly the normal rate.
        haul = {"iron_ore": 1000, "copper_ore": 500, "coal": 200}
        out, _ = apply_mining_efficiency("iron", haul)
        self.assertEqual(out["copper_ore"], 500)
        self.assertGreater(out["iron_ore"], 1000)

    def test_the_boost_alone_exactly_doubles_both_recipe_inputs(self):
        # With the correction turned off, the boost is all that is left, and it
        # is uniform across the recipe's inputs by construction.
        haul = {"iron_ore": 1000, "coal": 200}
        with patch("data.materials.MINING_EFFICIENCY_CORRECTION_CAP", 0.0):
            out, _ = apply_mining_efficiency("steel", haul)
        self.assertEqual(out["iron_ore"], 2000)
        self.assertEqual(out["coal"], 400)

    def test_the_correction_can_reduce_the_item_count(self):
        """A Steel efficiency converts iron ore into coal, and a coal is worth
        3.78 iron ore, so the haul comes back denser and shorter. /collect has
        to report the received count separately for this reason - the same
        thing a Coal focus already does."""
        haul = {"iron_ore": 1000, "coal": 200}
        out, _ = apply_mining_efficiency("steel", haul)
        self.assertLess(sum(out.values()), 2 * sum(haul.values()))
        self.assertGreater(out["coal"], 400)

    def test_small_collections_sum_to_one_big_one(self):
        """Twenty single-item collections must come to what one twenty-item
        collection does - the carries are the only thing making that true."""
        for efficiency_id in ("iron", "copper", "steel"):
            with self.subTest(efficiency=efficiency_id):
                one_go, _ = apply_mining_efficiency(
                    efficiency_id, {"iron_ore": 40, "copper_ore": 40, "coal": 40}
                )
                piecemeal, carries = {}, {}
                for _ in range(40):
                    part, carries = apply_mining_efficiency(
                        efficiency_id, {"iron_ore": 1, "copper_ore": 1, "coal": 1}, carries
                    )
                    for material_id, qty in part.items():
                        piecemeal[material_id] = piecemeal.get(material_id, 0) + qty
                for material_id in set(one_go) | set(piecemeal):
                    self.assertLessEqual(
                        abs(one_go.get(material_id, 0) - piecemeal.get(material_id, 0)), 1,
                        f"{efficiency_id}: {material_id}",
                    )

    def test_an_empty_haul_is_left_alone(self):
        out, carries = apply_mining_efficiency("iron", {})
        self.assertEqual(out, {})
        self.assertEqual(carries, {})

    def test_a_haul_with_none_of_the_recipes_materials_is_left_alone(self):
        haul = {"copper_ore": 50}
        out, _ = apply_mining_efficiency("steel", haul)
        self.assertEqual(out, haul)


class EfficiencyDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_user_row(self.db, USER)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_no_row_reads_as_the_default_and_not_unlocked(self):
        efficiency_id, last_changed, unlocked = await get_efficiency(self.db, USER)
        self.assertEqual(efficiency_id, DEFAULT_MINING_EFFICIENCY)
        self.assertEqual(last_changed, "")
        self.assertFalse(unlocked)

    async def test_the_row_is_the_unlock(self):
        async with self.db.transaction() as tx:
            await set_efficiency(tx, USER, "steel", "2026-08-25")
        efficiency_id, last_changed, unlocked = await get_efficiency(self.db, USER)
        self.assertEqual(efficiency_id, "steel")
        self.assertEqual(last_changed, "2026-08-25")
        self.assertTrue(unlocked)

    async def test_an_unknown_efficiency_falls_back_to_the_default(self):
        await self.db.execute(
            "INSERT INTO user_mining_efficiency (user_id, efficiency_id) VALUES (?, ?)",
            (USER, "adamantium"),
        )
        efficiency_id, _, unlocked = await get_efficiency(self.db, USER)
        self.assertEqual(efficiency_id, DEFAULT_MINING_EFFICIENCY)
        self.assertTrue(unlocked)

    async def test_a_locked_player_gets_their_haul_back_untouched(self):
        async with self.db.transaction() as tx:
            out = await boost_haul(tx, USER, {"iron_ore": 100, "coal": 20})
        self.assertEqual(out, {"iron_ore": 100, "coal": 20})

    async def test_boost_haul_persists_the_carries(self):
        async with self.db.transaction() as tx:
            await set_efficiency(tx, USER, "steel", "2026-08-25")
            await boost_haul(tx, USER, {"iron_ore": 7, "coal": 3})
        rows = await self.db.fetchall(
            "SELECT material_id, carry FROM user_mining_efficiency_carry WHERE user_id = ?",
            (USER,),
        )
        self.assertTrue(any(row["carry"] > 0 for row in rows))

    async def test_changing_efficiency_clears_every_carry(self):
        async with self.db.transaction() as tx:
            await set_efficiency(tx, USER, "steel", "2026-08-25")
            await boost_haul(tx, USER, {"iron_ore": 7, "coal": 3})
        async with self.db.transaction() as tx:
            await set_efficiency(tx, USER, "iron", "2026-08-26")
        rows = await self.db.fetchall(
            "SELECT material_id FROM user_mining_efficiency_carry WHERE user_id = ?", (USER,)
        )
        self.assertEqual(rows, [])

    async def test_collecting_in_pieces_matches_collecting_at_once(self):
        async with self.db.transaction() as tx:
            await set_efficiency(tx, USER, "iron", "2026-08-25")
            piecemeal = {}
            for _ in range(30):
                part = await boost_haul(tx, USER, {"iron_ore": 1, "coal": 1})
                for material_id, qty in part.items():
                    piecemeal[material_id] = piecemeal.get(material_id, 0) + qty
        async with self.db.transaction() as tx:
            await set_efficiency(tx, USER, "iron", "2026-08-26")
            one_go = await boost_haul(tx, USER, {"iron_ore": 30, "coal": 30})
        for material_id in set(one_go) | set(piecemeal):
            self.assertLessEqual(
                abs(one_go.get(material_id, 0) - piecemeal.get(material_id, 0)), 1, material_id
            )

    async def test_the_unlock_cost_is_one_obsidian(self):
        self.assertEqual(MINING_EFFICIENCY_UNLOCK_COST, {"obsidian": 1})
        # A ruby is one per 11,111 items mined and an obsidian one per 111,111,
        # so the gate is deliberately ten times what a focus costs.
        self.assertAlmostEqual(
            RAW_MATERIALS["ruby"]["drop_chance"] / RAW_MATERIALS["obsidian"]["drop_chance"],
            10.0, places=6,
        )


class EfficiencyTableTests(unittest.TestCase):
    def test_every_option_names_a_real_smelted_material(self):
        for efficiency_id, info in MINING_EFFICIENCIES.items():
            produces = info["produces"]
            if efficiency_id == DEFAULT_MINING_EFFICIENCY:
                self.assertIsNone(produces)
            else:
                self.assertIn(produces, SMELTED_MATERIALS)
                self.assertEqual(info["name"], SMELTED_MATERIALS[produces]["name"])

    def test_every_option_has_a_blurb(self):
        for info in MINING_EFFICIENCIES.values():
            self.assertTrue(info["blurb"].strip())

    def test_the_default_is_present_and_does_nothing(self):
        self.assertIn(DEFAULT_MINING_EFFICIENCY, MINING_EFFICIENCIES)
        self.assertIsNone(MINING_EFFICIENCIES[DEFAULT_MINING_EFFICIENCY]["produces"])
