import random
import unittest

from data.materials import RAW_MATERIALS, roll_raw_material
from utils.drills import build_material_breakdown


class BuildMaterialBreakdownTests(unittest.TestCase):
    def test_builds_breakdown_from_total_items(self):
        seq = iter(["iron_ore", "iron_ore", "coal"])
        breakdown = build_material_breakdown(3, lambda: next(seq))
        self.assertEqual(breakdown, {"iron_ore": 2, "coal": 1})

    def test_returns_empty_breakdown_for_zero_items(self):
        self.assertEqual(build_material_breakdown(0, lambda: "iron_ore"), {})


class RollRawMaterialTests(unittest.TestCase):
    """The drop roll is a pure function of RAW_MATERIALS, so it can be pinned
    down exactly rather than sampled."""

    def test_only_ever_returns_a_raw_material(self):
        rng = random.Random(20260803)
        for _ in range(2000):
            self.assertIn(roll_raw_material(rng), RAW_MATERIALS)

    def test_roll_lands_in_the_band_it_falls_in(self):
        # Each material owns the interval between the running totals of the
        # drop chances before and after it, so a roll placed just inside a
        # band has to come back as that band's material.
        cumulative = 0.0
        for material_id, info in RAW_MATERIALS.items():
            lower, cumulative = cumulative, cumulative + info["drop_chance"]
            midpoint = (lower + cumulative) / 2
            self.assertEqual(roll_raw_material(_FixedRng(midpoint)), material_id)

    def test_a_roll_of_exactly_one_still_returns_something(self):
        # Drop chances sum to 1.0 only up to float rounding, so the very top of
        # the range can fall through every band. It must not return None.
        self.assertIn(roll_raw_material(_FixedRng(1.0)), RAW_MATERIALS)


class _FixedRng:
    """Stands in for the random module, returning one predetermined roll."""

    def __init__(self, value: float):
        self._value = value

    def random(self) -> float:
        return self._value


if __name__ == "__main__":
    unittest.main()
