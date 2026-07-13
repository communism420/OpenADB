from __future__ import annotations

import time
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from openadb.core.acbridge_p2p import P2P_MAX_PARALLELISM
from openadb.core.p2p_parallelism import (
    AUTO_PARALLELISM_MODE,
    MANUAL_PARALLELISM_MODE,
    P2P_AUTO_MAX_PARALLELISM,
    P2PParallelismPreference,
    choose_p2p_parallelism,
    migrate_p2p_parallelism_setting,
    normalize_p2p_parallelism_preference,
)


MIB = 1024 * 1024


class P2PParallelismPreferenceTests(unittest.TestCase):
    def test_legacy_values_one_through_eight_become_manual_overrides(self) -> None:
        for value in range(1, P2P_MAX_PARALLELISM + 1):
            with self.subTest(value=value):
                preference = migrate_p2p_parallelism_setting(value)
                self.assertEqual(preference.mode, MANUAL_PARALLELISM_MODE)
                self.assertEqual(preference.manual_value, value)
                self.assertEqual(preference.to_setting_value(), value)

    def test_invalid_legacy_values_migrate_to_auto(self) -> None:
        invalid_values = (
            None,
            0,
            -1,
            9,
            True,
            2.5,
            "invalid",
            "9" * 5000,
            object(),
        )
        for value in invalid_values:
            with self.subTest(value=value):
                preference = migrate_p2p_parallelism_setting(value)
                self.assertEqual(preference, P2PParallelismPreference())
                self.assertEqual(preference.to_setting_value(), AUTO_PARALLELISM_MODE)

    def test_new_and_structured_preferences_are_normalized(self) -> None:
        cases = (
            ("Auto (recommended)", None, AUTO_PARALLELISM_MODE, None),
            ("automatic", 7, AUTO_PARALLELISM_MODE, None),
            ("manual", "5", MANUAL_PARALLELISM_MODE, 5),
            ("fixed", 8, MANUAL_PARALLELISM_MODE, 8),
        )
        for mode, value, expected_mode, expected_value in cases:
            with self.subTest(mode=mode, value=value):
                preference = normalize_p2p_parallelism_preference(mode, value)
                self.assertEqual(preference.mode, expected_mode)
                self.assertEqual(preference.manual_value, expected_value)

        self.assertEqual(
            migrate_p2p_parallelism_setting(
                {"parallelism_mode": "fixed", "requested_parallelism": 4}
            ),
            P2PParallelismPreference(MANUAL_PARALLELISM_MODE, 4),
        )

    def test_preference_is_immutable(self) -> None:
        preference = P2PParallelismPreference()
        with self.assertRaises(FrozenInstanceError):
            preference.mode = MANUAL_PARALLELISM_MODE  # type: ignore[misc]

    def test_direct_model_creation_cannot_bypass_normalization(self) -> None:
        invalid_preferences = (
            (AUTO_PARALLELISM_MODE, 1),
            (MANUAL_PARALLELISM_MODE, None),
            (MANUAL_PARALLELISM_MODE, 0),
            (MANUAL_PARALLELISM_MODE, 9),
            ("fixed", 4),
        )
        for mode, value in invalid_preferences:
            with self.subTest(mode=mode, value=value), self.assertRaises(ValueError):
                P2PParallelismPreference(mode, value)


class P2PParallelismPlannerTests(unittest.TestCase):
    def test_single_file_always_uses_one_stream(self) -> None:
        for mode, manual in (("auto", None), ("manual", 8), ("invalid", 8)):
            with self.subTest(mode=mode, manual=manual):
                self.assertEqual(
                    choose_p2p_parallelism(
                        1, 8 * 1024 * MIB, 8 * 1024 * MIB, mode, manual
                    ),
                    1,
                )

    def test_auto_is_conservative_across_small_medium_and_large_plans(self) -> None:
        cases = (
            # A handful of files never exceeds their count.
            (2, 64 * MIB, 32 * MIB, 2),
            (4, 64 * MIB, 16 * MIB, 2),
            # Tiny files do not justify extra ACBridge session overhead.
            (100, 10 * MIB, 128 * 1024, 2),
            # A useful medium batch can use three streams.
            (8, 64 * MIB, 8 * MIB, 3),
            # Four streams require both many files and a substantial payload.
            (32, 256 * MIB, 8 * MIB, 4),
        )
        for file_count, total, largest, expected in cases:
            with self.subTest(file_count=file_count, total=total):
                self.assertEqual(
                    choose_p2p_parallelism(file_count, total, largest, "auto", None),
                    expected,
                )

    def test_dominant_file_caps_auto_at_two_streams(self) -> None:
        self.assertEqual(
            choose_p2p_parallelism(32, 1024 * MIB, 800 * MIB, "auto", None),
            2,
        )

    def test_streams_never_exceed_file_count_or_auto_cap(self) -> None:
        for file_count in range(1, 65):
            with self.subTest(file_count=file_count):
                selected = choose_p2p_parallelism(
                    file_count,
                    file_count * 64 * MIB,
                    64 * MIB,
                    "auto",
                    None,
                )
                self.assertGreaterEqual(selected, 1)
                self.assertLessEqual(selected, file_count)
                self.assertLessEqual(selected, P2P_AUTO_MAX_PARALLELISM)

    def test_manual_one_through_eight_are_honored_but_bounded_by_files(self) -> None:
        for manual in range(1, P2P_MAX_PARALLELISM + 1):
            with self.subTest(manual=manual):
                self.assertEqual(
                    choose_p2p_parallelism(20, 0, 0, "manual", manual),
                    manual,
                )
        self.assertEqual(choose_p2p_parallelism(3, 0, 0, "manual", 8), 3)

    def test_invalid_values_fail_closed_instead_of_raising(self) -> None:
        invalid_statistics = (
            (-1, 10, 10),
            (True, 10, 10),
            (4, -1, 1),
            (4, 10, -1),
            (4, 10, 11),
            (4, 0, 0),
        )
        for file_count, total, largest in invalid_statistics:
            with self.subTest(values=(file_count, total, largest)):
                self.assertEqual(
                    choose_p2p_parallelism(file_count, total, largest, "auto", None),
                    1,
                )

    def test_invalid_manual_override_falls_back_to_auto(self) -> None:
        self.assertEqual(
            choose_p2p_parallelism(32, 256 * MIB, 8 * MIB, "manual", 9),
            4,
        )

    def test_planning_benchmark_matrix_is_deterministic_and_has_no_network(
        self,
    ) -> None:
        plans = [
            (
                file_count,
                file_count * average,
                average,
                "auto",
                None,
            )
            for file_count in range(1, 65)
            for average in (64 * 1024, 1 * MIB, 8 * MIB, 64 * MIB)
        ]

        started = time.perf_counter()
        with patch(
            "openadb.core.acbridge_p2p.socket.create_connection",
            side_effect=AssertionError("planning must not use the network"),
        ):
            first = [
                choose_p2p_parallelism(*plan) for _ in range(100) for plan in plans
            ]
            second = [
                choose_p2p_parallelism(*plan) for _ in range(100) for plan in plans
            ]
        elapsed = time.perf_counter() - started

        self.assertEqual(first, second)
        self.assertTrue(
            all(1 <= result <= P2P_AUTO_MAX_PARALLELISM for result in first)
        )
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
