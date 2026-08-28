from __future__ import annotations

import stat
import tempfile
import unittest
import warnings
import zipfile
from hashlib import sha256
from pathlib import Path

from tools.build_license_bundle import build_license_bundle
from tools.verify_license_bundle import (
    LicenseBundleError,
    ZIP_CREATE_SYSTEM,
    ZIP_EXTERNAL_ATTR,
    ZIP_TIMESTAMP,
    verify_license_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
ROOT_LEGAL_FILES = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "THIRD_PARTY_SOURCES.md",
)
PLATFORM_TOOLS_NOTICE_ENTRY = "LICENSES/Android-Platform-Tools-37.0.0-NOTICE.txt"
ACBRIDGE_EMBEDDED_LEGAL_SOURCES = (
    ROOT
    / "openadb"
    / "resources"
    / "acbridge"
    / "third_party"
    / "shizuku-13.1.5"
    / "LICENSE-Shizuku-API.txt",
    ROOT
    / "openadb"
    / "resources"
    / "acbridge"
    / "third_party"
    / "desugar_jdk_libs-2.1.5"
    / "LICENSE-desugar_jdk_libs.txt",
    ROOT
    / "openadb"
    / "resources"
    / "acbridge"
    / "third_party"
    / "desugar_jdk_libs-2.1.5"
    / "LICENSE-configuration.txt",
)


def _workflow_step(workflow: str, name: str, next_name: str) -> str:
    start_marker = f"      - name: {name}"
    end_marker = f"      - name: {next_name}"
    start = workflow.index(start_marker)
    end = workflow.index(end_marker, start + len(start_marker))
    return workflow[start:end]


def _legal_bundle_fixture(root: Path) -> tuple[Path, Path, dict[str, bytes]]:
    licenses_root = root / "LICENSES"
    (licenses_root / "nested").mkdir(parents=True)
    expected = {
        "LICENSES/A-MIT.txt": b"MIT test license\n",
        "LICENSES/nested/B-Apache-2.0.txt": b"Apache test license\n",
        PLATFORM_TOOLS_NOTICE_ENTRY: b"Android Platform Tools notice\n",
    }
    for entry_name, data in expected.items():
        if entry_name == PLATFORM_TOOLS_NOTICE_ENTRY:
            continue
        destination = licenses_root / Path(entry_name).relative_to("LICENSES")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    platform_notice = root / "NOTICE.txt"
    platform_notice.write_bytes(expected[PLATFORM_TOOLS_NOTICE_ENTRY])
    return licenses_root, platform_notice, expected


def _write_bundle_variant(
    output: Path,
    payloads: dict[str, bytes],
    *,
    names: list[str] | None = None,
    compression: dict[str, int] | None = None,
    timestamps: dict[str, tuple[int, int, int, int, int, int]] | None = None,
    external_attributes: dict[str, int] | None = None,
) -> None:
    compression = compression or {}
    timestamps = timestamps or {}
    external_attributes = external_attributes or {}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in names if names is not None else sorted(payloads):
            info = zipfile.ZipInfo(name, date_time=timestamps.get(name, ZIP_TIMESTAMP))
            info.compress_type = compression.get(name, zipfile.ZIP_STORED)
            info.create_system = ZIP_CREATE_SYSTEM
            info.external_attr = external_attributes.get(name, ZIP_EXTERNAL_ATTR)
            archive.writestr(info, payloads[name])


class ReleaseLegalFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = (ROOT / "OpenADB.spec").read_text(encoding="utf-8")
        cls.windows_workflow = (
            ROOT / ".github" / "workflows" / "windows-build.yml"
        ).read_text(encoding="utf-8")
        cls.release_workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.privacy_policy = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
        cls.release_process = (ROOT / "docs" / "RELEASE_PROCESS.md").read_text(
            encoding="utf-8"
        )

    def test_reviewed_legal_sources_are_nonempty(self) -> None:
        for name in ROOT_LEGAL_FILES:
            source = ROOT / name
            self.assertTrue(source.is_file(), f"Missing legal document: {source}")
            data = source.read_bytes()
            self.assertTrue(data.strip(), f"Empty legal document: {source}")
            self.assertNotIn(b"\r", data, f"Legal document must use canonical LF: {source}")
            data.decode("utf-8")

        licenses_root = ROOT / "LICENSES"
        license_files = sorted(
            path for path in licenses_root.rglob("*") if path.is_file()
        )
        self.assertTrue(license_files, "LICENSES must contain reviewed license texts")
        for source in license_files:
            data = source.read_bytes()
            self.assertTrue(data.strip(), f"Empty license text: {source}")
            self.assertNotIn(b"\r", data, f"License text must use canonical LF: {source}")
            data.decode("utf-8")

        for source in ACBRIDGE_EMBEDDED_LEGAL_SOURCES:
            data = source.read_bytes()
            self.assertTrue(data.strip(), f"Empty ACBridge legal source: {source}")
            self.assertNotIn(
                b"\r",
                data,
                f"ACBridge legal source must use canonical LF: {source}",
            )

        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("* text=auto eol=lf", attributes)
        self.assertIn("*.apk binary", attributes)

    def test_qt_legal_snapshot_manifest_is_complete_and_exact(self) -> None:
        snapshot = ROOT / "LICENSES" / "Qt-6.11.1"
        manifest_path = snapshot / "SNAPSHOT_MANIFEST.sha256"
        actual: dict[str, str] = {}
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            digest, separator, name = line.partition("  ")
            self.assertEqual(separator, "  ", f"Malformed Qt manifest line: {line}")
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertNotIn(name, actual, f"Duplicate Qt manifest path: {name}")
            actual[name] = digest

        expected_paths = sorted(
            path.relative_to(snapshot).as_posix()
            for path in snapshot.rglob("*")
            if path.is_file() and path != manifest_path
        )
        self.assertEqual(list(actual), expected_paths)
        for name in expected_paths:
            self.assertEqual(
                actual[name],
                sha256((snapshot / name).read_bytes()).hexdigest(),
                f"Qt legal-source snapshot drifted: {name}",
            )

    def test_pyinstaller_spec_embeds_the_complete_checkout_bundle(self) -> None:
        compile(self.spec, "OpenADB.spec", "exec")
        self.assertIn(
            'LEGAL_ROOT_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md", '
            '"THIRD_PARTY_SOURCES.md")',
            self.spec,
        )
        self.assertIn('licenses_root = ROOT / "LICENSES"', self.spec)
        self.assertIn('licenses_root.rglob("*")', self.spec)
        self.assertIn('datas.append((str(source), "."))', self.spec)
        self.assertIn("source.relative_to(ROOT).parent.as_posix()", self.spec)
        self.assertIn("datas.append((str(source), destination))", self.spec)
        self.assertIn("source.stat().st_size <= 0", self.spec)

    def test_windows_build_has_byte_exact_executable_and_artifact_gates(self) -> None:
        self.assertIn("tests.test_release_legal_files", self.windows_workflow)
        self.assertIn('PYTHON_VERSION: "3.12.10"', self.windows_workflow)
        self.assertIn('python-version: "3.12.10"', self.windows_workflow)
        self.assertIn("python -m pip check", self.windows_workflow)
        self.assertIn(
            "-r requirements-bootstrap-win-py312.lock",
            self.windows_workflow,
        )
        self.assertIn(
            "-r requirements-build-win-py312.lock", self.windows_workflow
        )
        self.assertIn("--require-hashes", self.windows_workflow)
        self.assertIn("--no-cache-dir", self.windows_workflow)
        self.assertIn("--no-build-isolation", self.windows_workflow)
        self.assertIn("tools/verify_release_dependencies.py", self.windows_workflow)
        self.assertIn("tests.test_release_dependency_lock", self.windows_workflow)
        self.assertIn("bootstrap_lock_sha256", self.windows_workflow)
        self.assertIn("build_lock_sha256", self.windows_workflow)
        inspect_step = _workflow_step(
            self.windows_workflow,
            "Inspect bundled runtime payload",
            "Smoke-test the EXE with a clean temporary profile",
        )
        for name in ROOT_LEGAL_FILES:
            self.assertIn(f'"{name}"', inspect_step)
        self.assertIn(
            "from PyInstaller.archive.readers import CArchiveReader", inspect_step
        )
        self.assertIn('name.startswith("LICENSES/")', inspect_step)
        self.assertIn("actual_names != expected_names", inspect_step)
        self.assertIn("archive.extract(archive_names[name])", inspect_step)
        self.assertIn(
            'platform_notice_name = "platform-tools/NOTICE.txt"', inspect_step
        )
        self.assertIn(
            "archive.extract(archive_names[platform_notice_name]) != platform_notice_data",
            inspect_step,
        )
        self.assertIn("approved_apk_data = approved_apk.read_bytes()", inspect_step)
        self.assertIn("sha256(approved_apk_data).hexdigest()", inspect_step)
        self.assertIn(
            "archive.extract(archive_names[approved_apk_name]) != approved_apk_data",
            inspect_step,
        )
        for name in (
            "PySide6/Qt6Pdf.dll",
            "PySide6/plugins/imageformats/qpdf.dll",
            "libcrypto-3.dll",
            "libssl-3.dll",
            "python312.dll",
        ):
            self.assertIn(f'"{name}"', inspect_step)
        for name in (
            "PySide6/Qt6WebEngineCore.dll",
            "PySide6/Qt6WebEngineQuick.dll",
            "PySide6/Qt6WebEngineWidgets.dll",
            "PySide6/QtWebEngineCore.pyd",
            "PySide6/QtWebEngineQuick.pyd",
            "PySide6/QtWebEngineWidgets.pyd",
        ):
            self.assertIn(f'"{name}"', inspect_step)
        self.assertIn("unreviewed browser payload", inspect_step)

        checksum_step = _workflow_step(
            self.windows_workflow,
            "Write checksums and build status",
            "Upload verified Windows release artifact",
        )
        upload_step = self.windows_workflow[
            self.windows_workflow.index(
                "      - name: Upload verified Windows release artifact"
            ) :
        ]
        for name in (*ROOT_LEGAL_FILES, "LICENSES.zip"):
            self.assertIn(f'Join-Path $releaseDir "{name}"', checksum_step)
            self.assertIn(f"release/{name}", upload_step)
        self.assertIn("$expectedReleaseFiles", checksum_step)
        self.assertIn("Compare-Object", checksum_step)

        bundle_step = _workflow_step(
            self.windows_workflow,
            "Prepare unsigned release candidate",
            "Sign and verify when all Authenticode secrets are configured",
        )
        self.assertIn("tools/build_license_bundle.py", bundle_step)
        self.assertIn("tools/verify_license_bundle.py", bundle_step)

    def test_licenses_zip_builder_is_deterministic_and_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            licenses_root, platform_notice, expected_sources = _legal_bundle_fixture(
                temporary_root
            )
            first_zip = temporary_root / "first.zip"
            second_zip = temporary_root / "second.zip"
            for output in (first_zip, second_zip):
                build_license_bundle(licenses_root, platform_notice, output)
                verify_license_bundle(licenses_root, platform_notice, output)

            self.assertEqual(first_zip.read_bytes(), second_zip.read_bytes())
            with zipfile.ZipFile(first_zip) as archive:
                entries = archive.infolist()
                self.assertEqual(
                    [entry.filename for entry in entries], sorted(expected_sources)
                )
                for entry in entries:
                    self.assertEqual(entry.date_time, ZIP_TIMESTAMP)
                    self.assertEqual(entry.compress_type, zipfile.ZIP_STORED)
                    self.assertEqual(entry.create_system, ZIP_CREATE_SYSTEM)
                    self.assertEqual(
                        (entry.external_attr >> 16) & 0xFFFF,
                        stat.S_IFREG | 0o644,
                    )
                    self.assertEqual(
                        archive.read(entry), expected_sources[entry.filename]
                    )

    def test_license_bundle_verifier_rejects_deflate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            licenses_root, platform_notice, expected = _legal_bundle_fixture(root)
            bundle = root / "deflated.zip"
            target = "LICENSES/A-MIT.txt"
            _write_bundle_variant(
                bundle,
                expected,
                compression={target: zipfile.ZIP_DEFLATED},
            )
            with self.assertRaisesRegex(LicenseBundleError, "not ZIP_STORED"):
                verify_license_bundle(licenses_root, platform_notice, bundle)

    def test_license_bundle_verifier_rejects_tampered_platform_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            licenses_root, platform_notice, expected = _legal_bundle_fixture(root)
            bundle = root / "tampered.zip"
            tampered = dict(expected)
            tampered[PLATFORM_TOOLS_NOTICE_ENTRY] = b"substituted notice\n"
            _write_bundle_variant(bundle, tampered)
            with self.assertRaisesRegex(LicenseBundleError, "tampered content"):
                verify_license_bundle(licenses_root, platform_notice, bundle)

    def test_license_bundle_verifier_rejects_duplicate_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            licenses_root, platform_notice, expected = _legal_bundle_fixture(root)
            bundle = root / "duplicate.zip"
            target = "LICENSES/A-MIT.txt"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                _write_bundle_variant(
                    bundle,
                    expected,
                    names=[*sorted(expected), target],
                )
            with self.assertRaisesRegex(LicenseBundleError, "duplicate entries"):
                verify_license_bundle(licenses_root, platform_notice, bundle)

    def test_license_bundle_verifier_rejects_missing_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            licenses_root, platform_notice, expected = _legal_bundle_fixture(root)
            bundle = root / "missing.zip"
            missing = "LICENSES/A-MIT.txt"
            _write_bundle_variant(
                bundle,
                expected,
                names=[name for name in sorted(expected) if name != missing],
            )
            with self.assertRaisesRegex(LicenseBundleError, "entry set mismatch"):
                verify_license_bundle(licenses_root, platform_notice, bundle)

    def test_license_bundle_verifier_rejects_wrong_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            licenses_root, platform_notice, expected = _legal_bundle_fixture(root)
            bundle = root / "timestamp.zip"
            target = "LICENSES/A-MIT.txt"
            _write_bundle_variant(
                bundle,
                expected,
                timestamps={target: (2026, 8, 28, 1, 2, 4)},
            )
            with self.assertRaisesRegex(LicenseBundleError, "timestamp"):
                verify_license_bundle(licenses_root, platform_notice, bundle)

    def test_license_bundle_verifier_rejects_wrong_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            licenses_root, platform_notice, expected = _legal_bundle_fixture(root)
            bundle = root / "mode.zip"
            target = "LICENSES/A-MIT.txt"
            _write_bundle_variant(
                bundle,
                expected,
                external_attributes={target: (stat.S_IFREG | 0o600) << 16},
            )
            with self.assertRaisesRegex(LicenseBundleError, "file mode"):
                verify_license_bundle(licenses_root, platform_notice, bundle)

    def test_release_revalidates_and_publishes_every_legal_asset(self) -> None:
        validation_step = _workflow_step(
            self.release_workflow,
            "Validate build status, checksums, signature, and APK metadata",
            "Compose release notes from verified metadata",
        )
        publish_step = self.release_workflow[
            self.release_workflow.index(
                "      - name: Create signed release or clearly labelled unsigned preview"
            ) :
        ]
        for name in (*ROOT_LEGAL_FILES, "LICENSES.zip"):
            self.assertIn(f"'{name}'", validation_step)
            self.assertIn(f"'release/{name}'", publish_step)

        self.assertIn("$expectedBuildArtifactFiles", validation_step)
        self.assertIn("$expectedBuildChecksumNames", validation_step)
        self.assertIn("$checkoutHash", validation_step)
        self.assertIn("$artifactHash", validation_step)
        self.assertIn(
            "https://dl.google.com/android/repository/platform-tools_r37.0.0-win.zip",
            validation_step,
        )
        self.assertIn("f29bfb58d0d6f9a57d7dbcba6cc259f9ca6f58f1", validation_step)
        self.assertIn(
            "4fe305812db074cea32903a489d061eb4454cbc90a49e8fea677f4b7af764918",
            validation_step,
        )
        self.assertIn("tools/verify_license_bundle.py", validation_step)
        self.assertIn("$platformNoticePath", validation_step)
        self.assertIn("$expectedBootstrapLockHash", validation_step)
        self.assertIn("$expectedBuildLockHash", validation_step)
        self.assertIn("$expectedPublishFiles", validation_step)

    def test_public_signpath_pages_and_release_notes_are_complete_and_truthful(
        self,
    ) -> None:
        self.assertIn("## Downloads", self.readme)
        self.assertIn("## Code signing policy", self.readme)
        self.assertIn("[Code signing policy](#code-signing-policy)", self.readme)
        self.assertIn("[privacy policy](PRIVACY.md)", self.readme)
        self.assertIn(
            "Free code signing provided by [SignPath.io](https://about.signpath.io/)",
            self.readme,
        )
        self.assertIn(
            "certificate by [SignPath Foundation](https://signpath.org/)",
            self.readme,
        )
        for role in ("Authors:", "Committers and reviewers:", "Approvers:"):
            self.assertIn(role, self.readme)
        self.assertIn(
            "This program will not transfer any information to other networked "
            "systems unless specifically requested by the user or the person "
            "installing or operating it.",
            self.readme,
        )
        for actual_asset in (
            "OpenADB-3.1.0-unsigned.exe",
            "ACBridge-3.1.0.apk",
            "BUILD_STATUS.json",
            "SHA256SUMS.txt",
        ):
            self.assertIn(actual_asset, self.readme)
        self.assertIn("historical unsigned release", self.readme)
        self.assertIn("The next release produced from the current `main`", self.readme)
        self.assertNotIn("The release also provides", self.readme)

        privacy_policy_compact = " ".join(self.privacy_policy.split())
        self.assertIn("latest published `v3.1.0` release", privacy_policy_compact)
        self.assertIn(
            "historical `com.communism420.acbridge`", privacy_policy_compact
        )
        self.assertIn(
            "Unreleased builds from the current `main` branch use the permanent",
            privacy_policy_compact,
        )
        self.assertIn("not part of a signing request", privacy_policy_compact)
        self.assertIn(
            "runtime SignPath communication or telemetry", privacy_policy_compact
        )

        notes_step = _workflow_step(
            self.release_workflow,
            "Compose release notes from verified metadata",
            "Create signed release or clearly labelled unsigned preview",
        )
        for required_text in (
            "## Code signing policy",
            "OpenADB's current SignPath status",
            "Free code signing provided by [SignPath.io](https://about.signpath.io/)",
            "certificate by [SignPath Foundation](https://signpath.org/)",
            "Project roles (Authors, Committers and reviewers, and Approvers)",
            "README.md#code-signing-policy",
            "PRIVACY.md",
        ):
            self.assertIn(required_text, notes_step)
        self.assertIn(
            'https://github.com/communism420/OpenADB/blob/'
            '$($status.source_commit)/README.md#code-signing-policy',
            notes_step,
        )
        self.assertIn(
            'https://github.com/communism420/OpenADB/blob/'
            '$($status.source_commit)/PRIVACY.md',
            notes_step,
        )

        release_process_compact = " ".join(self.release_process.split())
        for invariant in (
            "Every rendered download/release page",
            "Code signing policy",
            "Free code signing provided by SignPath.io",
            "absolute link to the privacy policy",
            "Authors, committers/reviewers, and approver roles",
            "A metadata-only clarification",
            "must preserve the historical signed/unsigned state",
            "link an immutable commit containing",
        ):
            self.assertIn(invariant, release_process_compact)


if __name__ == "__main__":
    unittest.main()
