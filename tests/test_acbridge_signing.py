from __future__ import annotations

import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import build_acbridge


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_ROOT = ROOT / "openadb" / "resources" / "acbridge"


class ACBridgeSigningPolicyTests(unittest.TestCase):
    def test_private_signing_material_is_ignored_and_legacy_key_is_absent(self) -> None:
        ignored = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

        private_key_suffixes = {
            "*.keystore",
            "*.jks",
            "*.p12",
            "*.pfx",
            "*.pk8",
            "*.p8",
            "*.key",
            "*.pem",
        }
        self.assertTrue(private_key_suffixes.issubset(ignored))
        self.assertFalse((BRIDGE_ROOT / "openadb-debug.keystore").exists())

    def test_builder_has_no_debug_key_generation_or_hardcoded_password(self) -> None:
        source = (ROOT / "tools" / "build_acbridge.py").read_text(encoding="utf-8")

        self.assertNotIn("openadb-debug.keystore", source)
        self.assertNotIn("openadbdebug", source)
        self.assertNotIn("-genkeypair", source)
        self.assertNotIn("pass:android", source)
        self.assertIsNone(re.search(r'''["']android["']''', source))
        self.assertIn("env:{SIGNING_STORE_PASSWORD_ENV}", source)
        self.assertIn("env:{SIGNING_KEY_PASSWORD_ENV}", source)

        wrapper = (ROOT / "tools" / "build_acbridge_release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("ANDROID_BUILD_TOOLS_VERSION", wrapper)
        self.assertIn("ANDROID_PLATFORM_VERSION", wrapper)
        self.assertIn("'37.0.0'", wrapper)
        self.assertIn("'36'", wrapper)

    def test_unsigned_is_the_safe_default(self) -> None:
        arguments = build_acbridge.parse_args([])

        self.assertEqual(arguments.signing_mode, "unsigned")
        self.assertIsNone(arguments.output)

    def test_dex_packaging_is_independent_of_source_file_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_apk = root / "first.apk"
            second_apk = root / "second.apk"
            dex = root / "classes.dex"
            dex.write_bytes(b"deterministic-dex-payload")

            for apk in (first_apk, second_apk):
                with zipfile.ZipFile(apk, "w") as archive:
                    entry = zipfile.ZipInfo(
                        "AndroidManifest.xml",
                        date_time=build_acbridge.FIXED_ZIP_TIMESTAMP,
                    )
                    archive.writestr(entry, b"manifest")

            os.utime(dex, (1_600_000_000, 1_600_000_000))
            build_acbridge.append_dex_files(first_apk, [dex])
            os.utime(dex, (1_700_000_000, 1_700_000_000))
            build_acbridge.append_dex_files(second_apk, [dex])

            self.assertEqual(first_apk.read_bytes(), second_apk.read_bytes())
            with zipfile.ZipFile(first_apk) as archive:
                self.assertEqual(
                    archive.getinfo("classes.dex").date_time,
                    build_acbridge.FIXED_ZIP_TIMESTAMP,
                )

    def test_apk_normalization_removes_timezone_and_compression_variance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = (root / "first-input.apk", root / "second-input.apk")
            outputs = (root / "first-output.apk", root / "second-output.apk")
            for index, source in enumerate(sources):
                with zipfile.ZipFile(
                    source,
                    "w",
                    compression=(zipfile.ZIP_DEFLATED if index == 0 else zipfile.ZIP_STORED),
                ) as archive:
                    for name in ("classes.dex", "AndroidManifest.xml"):
                        entry = zipfile.ZipInfo(
                            name,
                            date_time=((2026, 8, 28, 3, 0, 0) if index == 0 else (1980, 1, 1, 0, 0, 0)),
                        )
                        entry.compress_type = (
                            zipfile.ZIP_DEFLATED if index == 0 else zipfile.ZIP_STORED
                        )
                        archive.writestr(entry, name.encode("ascii"))

            for source, output in zip(sources, outputs, strict=True):
                build_acbridge.normalize_apk_archive(source, output)

            self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
            with zipfile.ZipFile(outputs[0]) as archive:
                self.assertEqual(
                    archive.namelist(),
                    ["AndroidManifest.xml", "classes.dex"],
                )
                for entry in archive.infolist():
                    self.assertEqual(entry.date_time, build_acbridge.FIXED_ZIP_TIMESTAMP)
                    self.assertEqual(entry.compress_type, zipfile.ZIP_STORED)

    def test_alias_publication_rolls_back_if_second_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.apk"
            first = root / "ACBridge-3.1.0.apk"
            second = root / "ACBridge.apk"
            source.write_bytes(b"new-release")
            first.write_bytes(b"old-versioned")
            second.write_bytes(b"old-compatible")
            real_replace = os.replace
            first_publish_attempts = 0

            def replace_with_second_publish_failure(source_path, destination_path):
                nonlocal first_publish_attempts
                source_path = Path(source_path)
                destination_path = Path(destination_path)
                if (
                    destination_path == first
                    and source_path.name == f".{first.name}.openadb-stage"
                ):
                    first_publish_attempts += 1
                    if first_publish_attempts == 1:
                        transient_lock = PermissionError(
                            13,
                            "simulated Windows scanner lock",
                        )
                        transient_lock.winerror = 5
                        raise transient_lock
                if (
                    destination_path == second
                    and source_path.name == f".{second.name}.openadb-stage"
                ):
                    raise OSError("simulated second alias failure")
                return real_replace(source_path, destination_path)

            with (
                patch.object(
                    build_acbridge.os,
                    "replace",
                    side_effect=replace_with_second_publish_failure,
                ),
                patch.object(build_acbridge.time, "sleep", return_value=None) as sleep,
                self.assertRaisesRegex(OSError, "simulated second alias failure"),
            ):
                build_acbridge.atomic_publish_aliases(source, (first, second))

            self.assertEqual(first_publish_attempts, 2)
            sleep.assert_called_once_with(build_acbridge.WINDOWS_REPLACE_RETRY_DELAYS[0])
            self.assertEqual(first.read_bytes(), b"old-versioned")
            self.assertEqual(second.read_bytes(), b"old-compatible")
            self.assertEqual(list(root.glob(".*.openadb-*")), [])

    def test_ci_can_pin_an_exact_sdk_component_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "build-tools"
            older = parent / "36.1.0"
            exact = parent / "37.0.0"
            newer = parent / "38.0.0"
            for directory in (older, exact, newer):
                directory.mkdir(parents=True)

            with patch.dict(
                os.environ,
                {"ANDROID_BUILD_TOOLS_VERSION": "37.0.0"},
                clear=False,
            ):
                selected = build_acbridge.selected_sdk_dir(
                    parent,
                    version_environment="ANDROID_BUILD_TOOLS_VERSION",
                )

            self.assertEqual(selected, exact)

    def test_missing_exact_sdk_component_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "platforms"
            (parent / "android-36.1").mkdir(parents=True)
            with (
                patch.dict(
                    os.environ,
                    {"ANDROID_PLATFORM_VERSION": "36"},
                    clear=False,
                ),
                self.assertRaisesRegex(SystemExit, "configured SDK component"),
            ):
                build_acbridge.selected_sdk_dir(
                    parent,
                    version_environment="ANDROID_PLATFORM_VERSION",
                    name_prefix="android-",
                )

    def test_release_mode_requires_every_external_signing_input(self) -> None:
        signing_names = (
            build_acbridge.SIGNING_KEYSTORE_ENV,
            build_acbridge.SIGNING_STORE_PASSWORD_ENV,
            build_acbridge.SIGNING_KEY_PASSWORD_ENV,
            build_acbridge.SIGNING_ALIAS_ENV,
        )
        with patch.dict(os.environ, {name: "" for name in signing_names}, clear=False):
            with self.assertRaisesRegex(SystemExit, "external signing environment"):
                build_acbridge.release_signing_config("keytool")

    def test_release_mode_rejects_a_keystore_inside_the_repository(self) -> None:
        environment = {
            build_acbridge.SIGNING_KEYSTORE_ENV: str(build_acbridge.PUBLIC_CERTIFICATE),
            build_acbridge.SIGNING_STORE_PASSWORD_ENV: "private-store-value",
            build_acbridge.SIGNING_KEY_PASSWORD_ENV: "private-key-value",
            build_acbridge.SIGNING_ALIAS_ENV: "openadb-release",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaisesRegex(SystemExit, "outside the repository"):
                build_acbridge.release_signing_config("keytool")

    def test_release_key_passwords_are_referenced_by_environment_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            keystore = Path(temporary) / "release.p12"
            keystore.write_bytes(b"test-keystore-placeholder")
            environment = {
                build_acbridge.SIGNING_KEYSTORE_ENV: str(keystore),
                build_acbridge.SIGNING_STORE_PASSWORD_ENV: "private-store-value",
                build_acbridge.SIGNING_KEY_PASSWORD_ENV: "private-key-value",
                build_acbridge.SIGNING_ALIAS_ENV: "openadb-release",
            }
            commands: list[list[str]] = []

            def fake_run(command: list[object]) -> None:
                rendered = [str(part) for part in command]
                commands.append(rendered)
                output = Path(rendered[rendered.index("-file") + 1])
                output.write_bytes(build_acbridge.PUBLIC_CERTIFICATE.read_bytes())

            with (
                patch.dict(os.environ, environment, clear=False),
                patch.object(build_acbridge, "run", side_effect=fake_run),
            ):
                config = build_acbridge.release_signing_config("keytool")

        self.assertEqual(config["keystore"], keystore.resolve())
        self.assertEqual(config["alias"], "openadb-release")
        rendered = " ".join(commands[0])
        self.assertIn("-storepass:env", rendered)
        self.assertIn(build_acbridge.SIGNING_STORE_PASSWORD_ENV, rendered)
        self.assertNotIn(environment[build_acbridge.SIGNING_STORE_PASSWORD_ENV], rendered)
        self.assertNotIn(environment[build_acbridge.SIGNING_KEY_PASSWORD_ENV], rendered)

    def test_wrong_release_certificate_fails_closed_without_secret_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            keystore = Path(temporary) / "release.p12"
            keystore.write_bytes(b"test-keystore-placeholder")
            environment = {
                build_acbridge.SIGNING_KEYSTORE_ENV: str(keystore),
                build_acbridge.SIGNING_STORE_PASSWORD_ENV: "private-store-value",
                build_acbridge.SIGNING_KEY_PASSWORD_ENV: "private-key-value",
                build_acbridge.SIGNING_ALIAS_ENV: "openadb-release",
            }

            def fake_run(command: list[object]) -> None:
                rendered = [str(part) for part in command]
                output = Path(rendered[rendered.index("-file") + 1])
                output.write_bytes(b"different-certificate")

            with (
                patch.dict(os.environ, environment, clear=False),
                patch.object(build_acbridge, "run", side_effect=fake_run),
                self.assertRaises(SystemExit) as raised,
            ):
                build_acbridge.release_signing_config("keytool")

        message = str(raised.exception)
        self.assertIn("pinned release certificate", message)
        self.assertNotIn(environment[build_acbridge.SIGNING_STORE_PASSWORD_ENV], message)
        self.assertNotIn(environment[build_acbridge.SIGNING_KEY_PASSWORD_ENV], message)

    def test_every_acbridge_feature_entrypoint_uses_the_exact_apk_trust_barrier(self) -> None:
        bridge_source = (ROOT / "openadb" / "core" / "acbridge.py").read_text(
            encoding="utf-8"
        )
        p2p_source = (ROOT / "openadb" / "core" / "acbridge_p2p.py").read_text(
            encoding="utf-8"
        )
        shizuku_source = (ROOT / "openadb" / "core" / "shizuku.py").read_text(
            encoding="utf-8"
        )

        # The sole direct call is the internal implementation of
        # ensure_trusted(); feature entry points must not bypass it.
        self.assertEqual(bridge_source.count("self.ensure_installed("), 1)
        self.assertNotIn("bridge.ensure_installed(", p2p_source)
        self.assertNotIn("bridge.ensure_installed(", shizuku_source)
        self.assertIn("self.bridge.ensure_trusted(", p2p_source)
        self.assertIn("self.bridge.ensure_trusted(", shizuku_source)

    def test_protected_apk_is_the_only_windows_and_release_input(self) -> None:
        acbridge_workflow = (
            ROOT / ".github" / "workflows" / "acbridge-release.yml"
        ).read_text(encoding="utf-8")
        windows_workflow = (
            ROOT / ".github" / "workflows" / "windows-build.yml"
        ).read_text(encoding="utf-8")
        release_workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("workflow_call:", acbridge_workflow)
        self.assertNotIn("workflow_dispatch:", acbridge_workflow)
        self.assertIn("value: ${{ jobs.verify.outputs.artifact_name }}", acbridge_workflow)
        self.assertIn(
            '"acbridge-release-verified-$env:GITHUB_SHA-$env:GITHUB_RUN_ID-$env:GITHUB_RUN_ATTEMPT"',
            acbridge_workflow,
        )

        self.assertIn("approved_acbridge_artifact_name:", windows_workflow)
        self.assertNotIn("  push:\n    tags:", windows_workflow)
        self.assertIn(
            "name: ${{ inputs.approved_acbridge_artifact_name }}",
            windows_workflow,
        )
        self.assertIn("OPENADB_ACBRIDGE_APPROVAL_ARTIFACT_NAME", windows_workflow)
        self.assertIn('release/ACBridge-*.apk', windows_workflow)

        self.assertIn("uses: ./.github/workflows/acbridge-release.yml", release_workflow)
        acbridge_job = release_workflow[
            release_workflow.index("  acbridge-release:") :
            release_workflow.index("  windows-build:")
        ]
        self.assertIn("- prepare", acbridge_job)
        self.assertIn("- wait-for-ci", acbridge_job)
        protected_signer = acbridge_workflow[
            acbridge_workflow.index("  sign:") :
            acbridge_workflow.index("  verify:")
        ]
        self.assertIn("default_branch", protected_signer)
        self.assertIn("communism420/OpenADB", protected_signer)
        self.assertIn("/branches/$encodedDefaultBranch", protected_signer)
        self.assertIn("merge_base_commit.sha", protected_signer)
        self.assertIn("base_commit.sha", protected_signer)
        self.assertNotIn("head_commit", protected_signer)
        self.assertIn("commits reachable from the current default branch", protected_signer)
        self.assertIn("actions/workflows/ci.yml/runs", protected_signer)
        self.assertIn("successful exact-tag Windows CI", protected_signer)
        self.assertIn(
            "approved_acbridge_artifact_name: ${{ needs.acbridge-release.outputs.artifact_name }}",
            release_workflow,
        )
        self.assertIn(
            "name: ${{ needs.acbridge-release.outputs.artifact_name }}",
            release_workflow,
        )
        self.assertIn(
            "$apkSource = Join-Path $release 'ACBridge-3.1.0.apk'",
            release_workflow,
        )
        self.assertNotIn(
            "$apkSource = 'openadb/resources/acbridge/ACBridge-3.1.0.apk'",
            release_workflow,
        )


if __name__ == "__main__":
    unittest.main()
