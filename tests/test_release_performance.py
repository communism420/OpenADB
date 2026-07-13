from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.release_performance import (
    REPORT_SCHEMA,
    BenchmarkConfig,
    BenchmarkInvariantError,
    generated_apps,
    measure,
    run_benchmarks,
    sanitized_environment,
    validate_release_profile,
    validate_report,
    write_json_report,
)


class ReleasePerformanceTests(unittest.TestCase):
    def small_config(self) -> BenchmarkConfig:
        return BenchmarkConfig(
            warmups=1,
            repetitions=2,
            app_counts=(12, 30),
            file_manager_entries=20,
            transfer_files=12,
            auto_stream_cases=16,
            stale_result_checks=16,
            operation_cycles=10,
        )

    def test_generated_apps_are_stable_and_cover_filter_dimensions(self) -> None:
        first = generated_apps(30)
        second = generated_apps(30)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 30)
        self.assertEqual({app.app_type for app in first}, {"user", "system"})
        self.assertEqual({app.state for app in first}, {"enabled", "disabled"})
        self.assertEqual(
            {app.bloatware_removal for app in first},
            {"", "Recommended", "Advanced", "Expert", "Unsafe"},
        )

    def test_measure_uses_warmups_repetitions_and_rejects_unstable_results(self) -> None:
        calls = 0

        def stable() -> int:
            nonlocal calls
            calls += 1
            return 7

        measurement = measure(stable, warmups=2, repetitions=3)
        self.assertEqual(calls, 5)
        self.assertEqual(measurement.checksum, 7)
        self.assertGreaterEqual(measurement.maximum_ms, measurement.average_ms)

        changing = iter((1, 2, 3))
        with self.assertRaises(BenchmarkInvariantError):
            measure(lambda: next(changing), warmups=1, repetitions=2)

    def test_small_run_covers_every_release_scenario_and_cleans_temp_data(self) -> None:
        with tempfile.TemporaryDirectory() as raw_parent:
            parent = Path(raw_parent)
            report = run_benchmarks(self.small_config(), temporary_parent=parent)
            self.assertEqual(list(parent.iterdir()), [])

        self.assertEqual(report["schema"], REPORT_SCHEMA)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            report["cleanup"],
            {"temporary_workspace_removed": True},
        )
        results = report["results"]
        self.assertIsInstance(results, list)
        scenario_names = {result["scenario"] for result in results}
        self.assertEqual(
            scenario_names,
            {
                "applications.filter",
                "applications.sort_name",
                "applications.sort_size",
                "applications.selection",
                "applications.metadata_progress",
                "file_manager.large_local_tree",
                "file_manager.transfer_plan",
                "p2p.auto_streams",
                "controllers.stale_result_filter",
                "operations.register_finish",
            },
        )
        app_results = [
            result for result in results if str(result["scenario"]).startswith("applications.")
        ]
        self.assertEqual({result["row_count"] for result in app_results}, {12, 30})
        for result in results:
            self.assertGreater(result["row_count"], 0)
            self.assertGreaterEqual(result["average_ms"], 0)
            self.assertGreaterEqual(result["max_ms"], result["average_ms"])
            self.assertTrue(result["method"])

    def test_environment_and_json_report_exclude_identity_and_paths(self) -> None:
        private_name = "private" + "-user"
        with (
            patch("platform.node", side_effect=AssertionError("hostname must not be read")),
            patch.dict(
                "os.environ",
                {
                    "USERNAME": private_name,
                    "USERPROFILE": "C:/" + "Users/" + private_name,
                    "HOME": "/" + "home/" + private_name,
                },
                clear=False,
            ),
        ):
            environment = sanitized_environment("physical")

        serialized_environment = json.dumps(environment).casefold()
        self.assertNotIn(private_name, serialized_environment)
        self.assertEqual(environment["environment_type"], "physical")
        self.assertTrue(environment["pyside6_version"])
        self.assertFalse(
            {"hostname", "username", "user", "home", "cwd", "path"}.intersection(
                environment
            )
        )

        with tempfile.TemporaryDirectory() as raw_directory:
            target = Path(raw_directory) / "private-filename.json"
            report = run_benchmarks(self.small_config())
            write_json_report(target, report)
            serialized_report = target.read_text(encoding="utf-8")
            loaded = json.loads(serialized_report)

        self.assertEqual(loaded["schema"], REPORT_SCHEMA)
        self.assertNotIn("private-filename", serialized_report)
        self.assertNotIn(raw_directory, serialized_report)

    def test_invalid_configuration_and_report_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            BenchmarkConfig(repetitions=1).validate()
        with self.assertRaises(ValueError):
            BenchmarkConfig(file_manager_entries=10, transfer_files=10).validate()
        with self.assertRaises(BenchmarkInvariantError):
            validate_report({"schema": REPORT_SCHEMA, "status": "passed"})
        with self.assertRaises(ValueError):
            sanitized_environment("personal-computer")

        report = run_benchmarks(self.small_config())
        with self.assertRaises(BenchmarkInvariantError):
            validate_release_profile(report)

        report = run_benchmarks(self.small_config())
        results = report["results"]
        assert isinstance(results, list)
        first_result = results[0]
        assert isinstance(first_result, dict)
        first_result["average_ms"] = float("nan")
        with self.assertRaises(BenchmarkInvariantError):
            validate_report(report)

        first_result["average_ms"] = 0.0
        first_result["row_count"] = "12"
        with self.assertRaises(BenchmarkInvariantError):
            validate_report(report)


if __name__ == "__main__":
    unittest.main()
