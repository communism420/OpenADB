from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from openadb.core.acbridge_p2p import P2P_MAX_PARALLELISM
from openadb.core.device_context import DeviceContext
from openadb.core.transfer_plan import (
    ADB_TRANSFER,
    AUTO_PARALLELISM,
    FIXED_PARALLELISM,
    P2P_TRANSFER,
    PULL_DIRECTION,
    PUSH_DIRECTION,
    TransferPlan,
    TransferPlanError,
)


def make_context() -> DeviceContext:
    return DeviceContext(
        serial="transport-42",
        mode="ADB",
        transport_id="42",
        profile_key="usb:phone",
        profile_kind="usb",
        profile_path=Path("profiles/phone"),
        backups_path=Path("profiles/phone/backups"),
        temp_path=Path("profiles/phone/temp"),
        logs_path=Path("profiles/phone/logs"),
        generation=7,
    )


class MutablePath:
    def __init__(self, value: str) -> None:
        self.value = value

    def __fspath__(self) -> str:
        return self.value


class TransferPlanTests(unittest.TestCase):
    def test_plan_is_frozen_and_copies_mutable_path_inputs(self) -> None:
        source = MutablePath("C:/source.bin")
        destination = MutablePath("/sdcard/Download")
        sources = [source]
        plan = TransferPlan(
            direction="PC → Android",
            transport="P2P",
            sources=sources,  # type: ignore[arg-type]
            destination=destination,  # type: ignore[arg-type]
            device_context=make_context(),
            use_root=False,
            requested_parallelism=4,
        )

        source.value = "C:/changed.bin"
        destination.value = "/storage/changed"
        sources.append(MutablePath("C:/extra.bin"))

        self.assertEqual(plan.direction, PUSH_DIRECTION)
        self.assertEqual(plan.transport, P2P_TRANSFER)
        self.assertEqual(plan.sources, ("C:/source.bin",))
        self.assertEqual(plan.destination, "/sdcard/Download")
        self.assertEqual(plan.fixed_parallelism(), 4)
        with self.assertRaises(FrozenInstanceError):
            plan.destination = "/sdcard/Other"  # type: ignore[misc]

    def test_selector_root_destination_and_device_changes_do_not_affect_plan(self) -> None:
        transport_selector = ADB_TRANSFER
        root_checked = True
        destination_field = "/sdcard/Original"
        context = make_context()
        plan = TransferPlan(
            direction="push",
            transport=transport_selector,
            sources=("C:/payload.zip",),
            destination=destination_field,
            device_context=context,
            use_root=root_checked,
            requested_parallelism=1,
        )

        transport_selector = P2P_TRANSFER
        root_checked = False
        destination_field = "/sdcard/Changed"
        replacement_context = make_context()
        object.__setattr__(replacement_context, "serial", "other-device")

        self.assertEqual(plan.transport, ADB_TRANSFER)
        self.assertTrue(plan.use_root)
        self.assertEqual(plan.destination, "/sdcard/Original")
        self.assertEqual(plan.device_context.serial, "transport-42")

    def test_single_source_string_is_not_split_into_characters(self) -> None:
        plan = TransferPlan(
            direction="pull",
            transport="platform tools",
            sources="/sdcard/file.txt",  # type: ignore[arg-type]
            destination=Path("C:/Downloads"),  # type: ignore[arg-type]
            device_context=make_context(),
        )
        self.assertEqual(plan.sources, ("/sdcard/file.txt",))
        self.assertEqual(plan.direction, PULL_DIRECTION)
        self.assertTrue(plan.is_download)

    def test_auto_parallelism_can_defer_stream_choice(self) -> None:
        plan = TransferPlan(
            direction="upload",
            transport="acbridge_p2p",
            sources=("C:/one.bin",),
            destination="/storage/emulated/0/Download",
            device_context=make_context(),
            parallelism_mode="automatic",
            requested_parallelism=None,
        )
        self.assertEqual(plan.parallelism_mode, AUTO_PARALLELISM)
        self.assertIsNone(plan.requested_parallelism)
        self.assertEqual(plan.fixed_parallelism(automatic_default=3), 3)

    def test_parallelism_uses_existing_p2p_limit(self) -> None:
        with self.assertRaisesRegex(TransferPlanError, str(P2P_MAX_PARALLELISM)):
            TransferPlan(
                direction="push",
                transport="p2p",
                sources=("C:/one.bin",),
                destination="/sdcard/",
                device_context=make_context(),
                requested_parallelism=P2P_MAX_PARALLELISM + 1,
            )

    def test_invalid_inputs_fail_before_transfer_starts(self) -> None:
        base = {
            "direction": "push",
            "transport": "adb",
            "sources": ("C:/one.bin",),
            "destination": "/sdcard/",
            "device_context": make_context(),
        }
        invalid_overrides = (
            {"direction": "sideways"},
            {"transport": "ftp"},
            {"sources": ()},
            {"sources": (None,)},
            {"destination": None},
            {"destination": "/sdcard/bad\nname"},
            {"use_root": "yes"},
            {"parallelism_mode": FIXED_PARALLELISM, "requested_parallelism": None},
            {"requested_parallelism": 0},
            {"requested_parallelism": 1.5},
        )
        for override in invalid_overrides:
            with self.subTest(override=override), self.assertRaises(TransferPlanError):
                TransferPlan(**(base | override))  # type: ignore[arg-type]

    def test_p2p_pull_is_rejected_until_protocol_supports_it(self) -> None:
        with self.assertRaisesRegex(TransferPlanError, "PC to Android"):
            TransferPlan(
                direction="pull",
                transport="p2p",
                sources=("/sdcard/file.bin",),
                destination="C:/Downloads",
                device_context=make_context(),
            )


if __name__ == "__main__":
    unittest.main()
