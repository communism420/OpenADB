from __future__ import annotations

# ruff: noqa: E402 -- direct execution adds the repository root before imports.

import argparse
import json
import math
import os
import platform
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
from statistics import fmean
from time import perf_counter_ns
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openadb.core.app_metadata_loader import AppMetadataLoader
from openadb.core.device_context import DeviceContext
from openadb.core.file_listing_controller import (
    AndroidListingRequest,
    FileListingController,
)
from openadb.core.operations import OperationRegistry
from openadb.core.p2p_parallelism import choose_p2p_parallelism
from openadb.core.transfer_plan import (
    AUTO_PARALLELISM,
    P2P_TRANSFER,
    PUSH_DIRECTION,
    TransferPlan,
)
from openadb.models.app_info import AppInfo
from openadb.ui.app_selection_model import AppSelectionModel
from openadb.ui.widgets.app_list_widget import AppFilterState, AppTable
from openadb.version import VERSION


REPORT_SCHEMA = "openadb.release-performance.v1"
DEFAULT_APP_COUNTS = (1_200, 3_000)
ENVIRONMENT_TYPES = ("physical", "virtual-machine", "container", "unknown")


class BenchmarkInvariantError(RuntimeError):
    """Raised when a benchmark stops measuring the intended safe operation."""


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    warmups: int = 2
    repetitions: int = 7
    app_counts: tuple[int, ...] = DEFAULT_APP_COUNTS
    file_manager_entries: int = 5_000
    transfer_files: int = 3_000
    auto_stream_cases: int = 4_096
    stale_result_checks: int = 4_096
    operation_cycles: int = 2_000

    def validate(self) -> None:
        if not 1 <= self.warmups <= 10:
            raise ValueError("warmups must be between 1 and 10")
        if not 2 <= self.repetitions <= 20:
            raise ValueError("repetitions must be between 2 and 20")
        if not self.app_counts or any(count < 1 for count in self.app_counts):
            raise ValueError("app counts must be positive")
        positive_counts = (
            self.file_manager_entries,
            self.transfer_files,
            self.auto_stream_cases,
            self.stale_result_checks,
            self.operation_cycles,
        )
        if any(count < 1 for count in positive_counts):
            raise ValueError("benchmark row counts must be positive")
        generated_files = self.file_manager_entries - _directory_count(
            self.file_manager_entries
        )
        if self.transfer_files > generated_files:
            raise ValueError("transfer_files exceeds the generated local file count")


@dataclass(frozen=True, slots=True)
class Measurement:
    average_ms: float
    maximum_ms: float
    checksum: int


class _GeneratedMetadataClient:
    """In-memory metadata source; deliberately has no Android transport."""

    def get_package_details_many(
        self,
        package_names: list[str],
        *,
        max_workers: int,
        progress_callback=None,
        cancel_event=None,
    ) -> dict[str, dict[str, str]]:
        del max_workers
        total = len(package_names)
        result: dict[str, dict[str, str]] = {}
        for index, package_name in enumerate(package_names, start=1):
            if cancel_event is not None and cancel_event.is_set():
                break
            details = {
                "appLabel": f"Generated application {index}",
                "versionName": "1.0",
                "versionCode": str(index),
                "sizeBytes": str(index * 1_024),
            }
            result[package_name] = details
            if progress_callback is not None:
                progress_callback(index, total, package_name, details)
        return result


class _CurrentContextManager:
    def __init__(self, current: DeviceContext) -> None:
        self.current = current

    def is_context_current(self, context: DeviceContext) -> bool:
        return context == self.current


def generated_apps(count: int) -> list[AppInfo]:
    """Return stable mock application rows without touching settings or devices."""

    categories = ("Recommended", "Advanced", "Expert", "Unsafe", "")
    apps: list[AppInfo] = []
    for index in range(count):
        size = (
            "Unknown"
            if index % 11 == 0
            else f"{(index % 997) + 1}.5 MB"
            if index % 2
            else f"{(index % 4_093) + 1} KB"
        )
        apps.append(
            AppInfo(
                package_name=f"org.openadb.generated.app{index:05d}",
                app_label=f"Generated application {count - index:05d}",
                app_type="system" if index % 2 == 0 else "user",
                state="disabled" if index % 3 == 0 else "enabled",
                size=size,
                bloatware_removal=categories[index % len(categories)],
            )
        )
    return apps


def measure(
    operation: Callable[[], int],
    *,
    warmups: int,
    repetitions: int,
) -> Measurement:
    """Measure a deterministic operation and reject changing result checksums."""

    checksums: list[int] = []
    for _ in range(warmups):
        checksums.append(int(operation()))

    durations_ns: list[int] = []
    for _ in range(repetitions):
        started = perf_counter_ns()
        checksums.append(int(operation()))
        durations_ns.append(perf_counter_ns() - started)

    if len(set(checksums)) != 1:
        raise BenchmarkInvariantError("A measured operation returned unstable results")
    durations_ms = [duration / 1_000_000 for duration in durations_ns]
    return Measurement(
        average_ms=round(fmean(durations_ms), 6),
        maximum_ms=round(max(durations_ms), 6),
        checksum=checksums[0],
    )


def run_benchmarks(
    config: BenchmarkConfig | None = None,
    *,
    temporary_parent: Path | None = None,
    environment_type: str = "unknown",
) -> dict[str, object]:
    """Run release benchmarks using generated local data only.

    The function never constructs an ADB or fastboot client, starts a process,
    reads user settings, or contacts a device. Temporary entries are empty and
    are removed before the report is returned.
    """

    selected = config or BenchmarkConfig()
    selected.validate()
    if environment_type not in ENVIRONMENT_TYPES:
        raise ValueError("environment_type is not a supported sanitized value")
    results: list[dict[str, object]] = []

    for app_count in selected.app_counts:
        apps = generated_apps(app_count)
        results.extend(_application_measurements(apps, selected))

    results.append(_auto_stream_measurement(selected))
    results.append(_stale_result_measurement(selected))
    results.append(_operation_registry_measurement(selected))

    workspace: Path | None = None
    with tempfile.TemporaryDirectory(
        prefix="openadb-release-performance-",
        dir=str(temporary_parent) if temporary_parent is not None else None,
    ) as raw_workspace:
        workspace = Path(raw_workspace)
        generated_sources = _generate_local_tree(
            workspace,
            selected.file_manager_entries,
        )
        results.append(_file_manager_measurement(workspace, selected))
        results.append(
            _transfer_plan_measurement(
                generated_sources[: selected.transfer_files],
                selected,
            )
        )

    cleanup_verified = workspace is not None and not workspace.exists()
    if not cleanup_verified:
        raise BenchmarkInvariantError("The generated benchmark workspace was not removed")

    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "openadb_version": VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed",
        "environment": sanitized_environment(environment_type),
        "method": {
            "clock": "perf_counter_ns",
            "warmup_iterations": selected.warmups,
            "measured_repetitions": selected.repetitions,
            "dataset": "deterministic generated mock data and empty temporary entries",
            "scope": "local CPU and temporary filesystem only; no device tools invoked",
        },
        "results": results,
        "cleanup": {"temporary_workspace_removed": cleanup_verified},
    }
    validate_report(report)
    return report


def sanitized_environment(environment_type: str = "unknown") -> dict[str, object]:
    """Return only coarse, non-identifying environment properties."""

    if environment_type not in ENVIRONMENT_TYPES:
        raise ValueError("environment_type is not a supported sanitized value")
    try:
        pyside_version = distribution_version("PySide6")
    except PackageNotFoundError:
        pyside_version = "Unavailable"
    return {
        "environment_type": environment_type,
        "operating_system": platform.system() or "Unknown",
        "operating_system_release": platform.release() or "Unknown",
        "architecture": platform.machine() or "Unknown",
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "pyside6_version": pyside_version,
        "logical_cpu_count": os.cpu_count(),
    }


def validate_report(report: dict[str, object]) -> None:
    if report.get("schema") != REPORT_SCHEMA or report.get("status") != "passed":
        raise BenchmarkInvariantError("Performance report schema or status is invalid")
    environment = report.get("environment")
    if not isinstance(environment, dict):
        raise BenchmarkInvariantError("Performance report environment is invalid")
    forbidden_environment_keys = {
        "hostname",
        "username",
        "user",
        "home",
        "cwd",
        "path",
        "executable",
    }
    if forbidden_environment_keys.intersection(environment):
        raise BenchmarkInvariantError("Performance report contains identifying environment data")
    results = report.get("results")
    if not isinstance(results, list) or not results:
        raise BenchmarkInvariantError("Performance report contains no scenarios")
    scenario_counts: dict[str, int] = {}
    for result in results:
        if not isinstance(result, dict):
            raise BenchmarkInvariantError("Performance result is not an object")
        required = {"scenario", "row_count", "average_ms", "max_ms", "method"}
        if not required.issubset(result):
            raise BenchmarkInvariantError("Performance result is missing required fields")
        row_count = result["row_count"]
        if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 1:
            raise BenchmarkInvariantError("Performance result row count is invalid")
        try:
            average_ms = float(result["average_ms"])
            maximum_ms = float(result["max_ms"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise BenchmarkInvariantError("Performance duration is invalid") from exc
        if (
            not math.isfinite(average_ms)
            or not math.isfinite(maximum_ms)
            or average_ms < 0
            or maximum_ms < 0
        ):
            raise BenchmarkInvariantError("Performance duration is invalid")
        if maximum_ms < average_ms:
            raise BenchmarkInvariantError("Performance maximum is below its average")
        scenario = str(result["scenario"])
        scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1
    expected_counts = {
        "applications.filter": 2,
        "applications.sort_name": 2,
        "applications.sort_size": 2,
        "applications.selection": 2,
        "applications.metadata_progress": 2,
        "file_manager.large_local_tree": 1,
        "file_manager.transfer_plan": 1,
        "p2p.auto_streams": 1,
        "controllers.stale_result_filter": 1,
        "operations.register_finish": 1,
    }
    if scenario_counts != expected_counts:
        raise BenchmarkInvariantError("Performance report scenario coverage is invalid")


def validate_release_profile(report: dict[str, object]) -> None:
    """Require the exact row counts promised by the release benchmark CLI."""

    validate_report(report)
    results = report["results"]
    assert isinstance(results, list)
    rows_by_scenario: dict[str, set[int]] = {}
    for result in results:
        assert isinstance(result, dict)
        rows_by_scenario.setdefault(str(result["scenario"]), set()).add(
            int(result["row_count"])
        )
    expected = {
        "applications.filter": {1_200, 3_000},
        "applications.sort_name": {1_200, 3_000},
        "applications.sort_size": {1_200, 3_000},
        "applications.selection": {1_200, 3_000},
        "applications.metadata_progress": {1_200, 3_000},
        "file_manager.large_local_tree": {5_000},
        "file_manager.transfer_plan": {3_000},
        "p2p.auto_streams": {4_096},
        "controllers.stale_result_filter": {4_096},
        "operations.register_finish": {2_000},
    }
    if rows_by_scenario != expected:
        raise BenchmarkInvariantError("Release performance row counts are invalid")


def write_json_report(path: Path, report: dict[str, object]) -> None:
    """Atomically persist a validated report without embedding its path."""

    validate_report(report)
    target = path.expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                report,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _application_measurements(
    apps: list[AppInfo],
    config: BenchmarkConfig,
) -> list[dict[str, object]]:
    count = len(apps)
    filters = AppFilterState.from_values(
        search_text="generated",
        app_type="system",
        app_state="enabled",
        uad_category="recommended",
    )

    def filter_apps() -> int:
        return sum(
            filters.matches(app, _uad_category(app))
            for app in apps
        )

    def sort_by_name() -> int:
        ordered = sorted(
            apps,
            key=lambda app: (app.display_name.casefold(), app.package_name.casefold()),
        )
        return len(ordered) + len(ordered[0].package_name) + len(ordered[-1].package_name)

    def sort_by_size() -> int:
        def key(app: AppInfo) -> tuple[bool, int, str, str]:
            parsed = AppTable._size_sort_value(None, app.size)
            unknown = parsed < 0
            return (
                unknown,
                -parsed if not unknown else parsed,
                app.display_name.casefold(),
                app.package_name.casefold(),
            )

        ordered = sorted(apps, key=key)
        return len(ordered) + AppTable._size_sort_value(None, ordered[0].size)

    packages = tuple(app.package_name for app in apps)
    selected_packages = packages[::3]

    def select_apps() -> int:
        selection = AppSelectionModel()
        selection.select_visible(selected_packages)
        summary = selection.summary(packages[::2])
        selection.unselect_visible(packages[::5])
        return summary.total_selected + summary.visible_selected + len(selection)

    def metadata_progress() -> int:
        progress_count = 0
        item_count = 0

        def on_progress(_message: str) -> None:
            nonlocal progress_count
            progress_count += 1

        def on_item(_app: AppInfo) -> None:
            nonlocal item_count
            item_count += 1

        loader = AppMetadataLoader(_GeneratedMetadataClient(), configured_parallelism=6)
        loaded = loader.load(
            apps,
            progress_callback=on_progress,
            item_callback=on_item,
        )
        completed = sum(app.metadata_checked for app in loaded)
        return completed + progress_count + item_count

    definitions = (
        (
            "applications.filter",
            "AppFilterState.matches over generated application rows",
            filter_apps,
        ),
        (
            "applications.sort_name",
            "AppInfo display-name casefold sort with package tie-break",
            sort_by_name,
        ),
        (
            "applications.sort_size",
            "AppTable size parser with descending deterministic list sort",
            sort_by_size,
        ),
        (
            "applications.selection",
            "AppSelectionModel select, summarize, and unselect",
            select_apps,
        ),
        (
            "applications.metadata_progress",
            "AppMetadataLoader merge plus per-row progress callbacks",
            metadata_progress,
        ),
    )
    return [
        _result(
            scenario=scenario,
            row_count=count,
            method=method,
            measurement=measure(
                operation,
                warmups=config.warmups,
                repetitions=config.repetitions,
            ),
            repetitions=config.repetitions,
        )
        for scenario, method, operation in definitions
    ]


def _file_manager_measurement(
    workspace: Path,
    config: BenchmarkConfig,
) -> dict[str, object]:
    def list_tree() -> int:
        listing = FileListingController.list_windows(workspace)
        directories = sum(entry.is_dir for entry in listing.entries)
        if len(listing.entries) != config.file_manager_entries:
            raise BenchmarkInvariantError("Generated File Manager listing lost entries")
        return len(listing.entries) + directories

    measurement = measure(
        list_tree,
        warmups=config.warmups,
        repetitions=config.repetitions,
    )
    return _result(
        scenario="file_manager.large_local_tree",
        row_count=config.file_manager_entries,
        method="FileListingController.list_windows over generated empty entries",
        measurement=measurement,
        repetitions=config.repetitions,
    )


def _transfer_plan_measurement(
    sources: Sequence[Path],
    config: BenchmarkConfig,
) -> dict[str, object]:
    context = _generated_context()

    def build_plan() -> int:
        plan = TransferPlan(
            direction=PUSH_DIRECTION,
            transport=P2P_TRANSFER,
            sources=tuple(sources),
            destination="/storage/emulated/0/Download",
            device_context=context,
            use_root=False,
            parallelism_mode=AUTO_PARALLELISM,
            requested_parallelism=None,
        )
        return len(plan.sources) + int(plan.is_p2p) + int(plan.is_upload)

    measurement = measure(
        build_plan,
        warmups=config.warmups,
        repetitions=config.repetitions,
    )
    return _result(
        scenario="file_manager.transfer_plan",
        row_count=len(sources),
        method="immutable TransferPlan capture for generated local sources",
        measurement=measurement,
        repetitions=config.repetitions,
    )


def _auto_stream_measurement(config: BenchmarkConfig) -> dict[str, object]:
    mib = 1_048_576
    cases = (
        (1, 8 * mib, 8 * mib),
        (12, 64 * mib, 8 * mib),
        (32, 512 * mib, 32 * mib),
        (32, 512 * mib, 400 * mib),
    )

    def choose_streams() -> int:
        total = 0
        for index in range(config.auto_stream_cases):
            file_count, total_bytes, largest_bytes = cases[index % len(cases)]
            total += choose_p2p_parallelism(
                file_count,
                total_bytes,
                largest_bytes,
                "auto",
                None,
            )
        return total

    measurement = measure(
        choose_streams,
        warmups=config.warmups,
        repetitions=config.repetitions,
    )
    return _result(
        scenario="p2p.auto_streams",
        row_count=config.auto_stream_cases,
        method="choose_p2p_parallelism over a fixed statistics matrix",
        measurement=measurement,
        repetitions=config.repetitions,
    )


def _stale_result_measurement(config: BenchmarkConfig) -> dict[str, object]:
    current_context = _generated_context()
    manager = _CurrentContextManager(current_context)
    controller = FileListingController(device_manager=manager)
    current = AndroidListingRequest(
        device_context=current_context,
        generation=controller.listing_generation,
        requested_path=controller.requested_android_path,
    )
    stale = AndroidListingRequest(
        device_context=current_context,
        generation=controller.listing_generation + 1,
        requested_path=controller.requested_android_path,
    )

    def filter_stale_results() -> int:
        accepted = 0
        for index in range(config.stale_result_checks):
            accepted += controller.is_listing_current(current if index % 2 == 0 else stale)
        return accepted

    measurement = measure(
        filter_stale_results,
        warmups=config.warmups,
        repetitions=config.repetitions,
    )
    return _result(
        scenario="controllers.stale_result_filter",
        row_count=config.stale_result_checks,
        method="FileListingController.is_listing_current on alternating generations",
        measurement=measurement,
        repetitions=config.repetitions,
    )


def _operation_registry_measurement(config: BenchmarkConfig) -> dict[str, object]:
    def register_and_finish() -> int:
        registry = OperationRegistry()
        finished = 0
        for index in range(config.operation_cycles):
            token = registry.register(
                "performance.validation",
                operation_id=f"generated-operation-{index}",
            )
            finished += registry.finish(token)
        if registry.active_count != 0:
            raise BenchmarkInvariantError("OperationRegistry retained generated tokens")
        return finished

    measurement = measure(
        register_and_finish,
        warmups=config.warmups,
        repetitions=config.repetitions,
    )
    return _result(
        scenario="operations.register_finish",
        row_count=config.operation_cycles,
        method="OperationRegistry register and finish cycles",
        measurement=measurement,
        repetitions=config.repetitions,
    )


def _generate_local_tree(workspace: Path, entry_count: int) -> list[Path]:
    directory_count = _directory_count(entry_count)
    for index in range(directory_count):
        (workspace / f"generated-directory-{index:04d}").mkdir()
    files: list[Path] = []
    for index in range(entry_count - directory_count):
        path = workspace / f"generated-entry-{index:05d}.bin"
        path.touch()
        files.append(path)
    return files


def _directory_count(entry_count: int) -> int:
    return min(256, max(1, entry_count // 10))


def _generated_context() -> DeviceContext:
    relative = Path("generated-profile")
    return DeviceContext(
        serial="generated-transport",
        mode="ADB",
        transport_id="generated-transport-id",
        profile_key="generated-profile",
        profile_kind="generated",
        profile_path=relative,
        backups_path=relative / "backups",
        temp_path=relative / "temporary",
        logs_path=relative / "logs",
        generation=1,
    )


def _uad_category(app: AppInfo) -> str:
    value = str(app.bloatware_removal or "").strip()
    if value in {"Recommended", "Advanced", "Expert", "Unsafe"}:
        return value
    return "Not listed"


def _result(
    *,
    scenario: str,
    row_count: int,
    method: str,
    measurement: Measurement,
    repetitions: int,
) -> dict[str, object]:
    return {
        "scenario": scenario,
        "row_count": row_count,
        "repetitions": repetitions,
        "average_ms": measurement.average_ms,
        "max_ms": measurement.maximum_ms,
        "method": method,
        "result_checksum": measurement.checksum,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic OpenADB release-performance checks with generated "
            "local data only. No ADB or fastboot command is executed."
        )
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        help="Write the sanitized JSON report to this path instead of stdout.",
    )
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=7)
    parser.add_argument(
        "--environment-type",
        choices=ENVIRONMENT_TYPES,
        default="unknown",
        help="Record a coarse, non-identifying execution environment type.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = BenchmarkConfig(
            warmups=args.warmups,
            repetitions=args.repetitions,
        )
        report = run_benchmarks(config, environment_type=args.environment_type)
        validate_release_profile(report)
        if args.json_report is not None:
            write_json_report(args.json_report, report)
        else:
            print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True))
    except (BenchmarkInvariantError, OSError, ValueError) as exc:
        print(f"Release performance validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
