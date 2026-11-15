"""Power distribution confirmation without game or OCR dependencies."""

import unittest
from unittest.mock import Mock, call

from controls.power import set_power_distribution


class PowerDistributionTestCase(unittest.TestCase):
    def test_travel_preserves_already_correct_distribution_without_inputs(self):
        keys, status = Mock(), Mock()
        status.get_cleaned_data.return_value = {"pips": {"system": 2.0, "engine": 4.0, "weapons": 0.0}}
        self.assertTrue(set_power_distribution(keys, status, "engine", skip_if_set=True))
        keys.send.assert_not_called()

    def test_travel_resets_and_confirms_before_setting_engine_priority(self):
        keys, status = Mock(), Mock()
        status.get_cleaned_data.side_effect = [
            {"pips": {"system": 4.0, "engine": 0.0, "weapons": 2.0}},
            {"pips": {"system": 2.0, "engine": 2.0, "weapons": 2.0}},
            {"pips": {"system": 2.0, "engine": 4.0, "weapons": 0.0}},
        ]
        self.assertTrue(set_power_distribution(keys, status, "engine", skip_if_set=True))
        self.assertEqual(
            keys.send.call_args_list,
            [
                call("ResetPowerDistribution"),
                call("IncreaseSystemsPower", repeat=4),
                call("IncreaseEnginesPower", repeat=6),
            ],
        )

    def test_unreadable_status_stops_without_further_power_inputs(self):
        for failure in ("initial", "reset", "final"):
            with self.subTest(failure=failure):
                keys, status = Mock(), Mock()
                states = [{"pips": {"system": 4.0, "engine": 0.0, "weapons": 2.0}}]
                if failure == "final":
                    states.append({"pips": {"system": 2.0, "engine": 2.0, "weapons": 2.0}})
                status.get_cleaned_data.side_effect = ([] if failure == "initial" else states) + [TimeoutError()]
                self.assertFalse(set_power_distribution(keys, status, "engine", skip_if_set=True))
                self.assertEqual(keys.send.call_count, {"initial": 0, "reset": 1, "final": 3}[failure])
