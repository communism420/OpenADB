from __future__ import annotations

import hashlib
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTION_COMMIT = "c92b958760219087e01f8d67a1669ed57afe2627"


def _job(workflow: str, name: str, next_name: str) -> str:
    start = workflow.index(f"  {name}:")
    end = workflow.index(f"  {next_name}:", start + len(name) + 3)
    return workflow[start:end]


class SignPathWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.windows = (
            ROOT / ".github" / "workflows" / "windows-build.yml"
        ).read_text(encoding="utf-8")
        cls.release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        cls.ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.privacy = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
        cls.setup = (ROOT / "docs" / "SIGNPATH_SETUP.md").read_text(
            encoding="utf-8"
        )
        cls.release_process = (ROOT / "docs" / "RELEASE_PROCESS.md").read_text(
            encoding="utf-8"
        )

    def test_pfx_path_is_removed_and_signpath_is_fail_closed(self) -> None:
        active_workflows = self.windows + self.release
        for retired_marker in (
            "WINDOWS_SIGNING_PFX_BASE64",
            "WINDOWS_SIGNING_PFX_PASSWORD",
            "WINDOWS_SIGNING_TIMESTAMP_URL",
            "openadb-signing.pfx",
            "signtool.FullName sign",
        ):
            self.assertNotIn(retired_marker, active_workflows)
        self.assertIn("needs.signpath-sign.result == 'success'", self.release)
        self.assertIn(
            "SignPath signing requests cannot be created from a workflow re-run",
            self.release,
        )
        self.assertNotIn("secrets: inherit", self.release)
        self.assertNotIn("allow_unsigned_stable", self.release)
        self.assertIn("No workflow input can promote", self.release_process)

    def test_duplicate_runs_and_noncanonical_repositories_are_rejected(self) -> None:
        self.assertIn(
            "group: openadb-release-v3.1.0",
            self.release,
        )
        self.assertIn("must not contain leading or trailing whitespace", self.release)
        self.assertIn("only by the primary immutable-tag push run", self.release)
        self.assertIn("communism420/OpenADB repository", self.release)
        signing_job = _job(self.release, "signpath-sign", "verify-release")
        self.assertIn("[int]$env:GITHUB_RUN_ATTEMPT -ne 1", signing_job)
        self.assertIn(
            "The protected SignPath job refuses every GitHub workflow re-run",
            signing_job,
        )
        self.assertIn(
            "Re-check the release boundary immediately before SignPath",
            signing_job,
        )
        self.assertIn(
            "The release tag changed immediately before the SignPath request",
            signing_job,
        )
        self.assertGreaterEqual(self.release.count("!cancelled()"), 2)

    def test_action_is_full_sha_pinned_and_token_never_enters_shell(self) -> None:
        action_ref = (
            "signpath/github-action-submit-signing-request@"
            f"{ACTION_COMMIT} # v2.3"
        )
        self.assertEqual(self.release.count(action_ref), 1)
        self.assertNotIn("signpath/github-action-submit-signing-request@v", self.release)
        secret_expression = "${{ secrets.SIGNPATH_API_TOKEN }}"
        self.assertEqual(self.release.count(secret_expression), 1)
        self.assertIn(f"api-token: {secret_expression}", self.release)
        self.assertNotIn(f"SIGNPATH_API_TOKEN: {secret_expression}", self.release)
        self.assertIn(f"SIGNPATH_ACTION_SHA: {ACTION_COMMIT}", self.release)
        self.assertIn(
            "SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA_SETTING: "
            "${{ vars.SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA }}",
            self.release,
        )
        self.assertIn(
            "$idempotencySetting -eq 'true' -and "
            "$reviewedActionSha -cne $env:SIGNPATH_ACTION_SHA",
            self.release,
        )

    def test_exact_one_file_artifact_id_crosses_the_signing_boundary(self) -> None:
        self.assertIn("id: upload_signpath_input", self.windows)
        self.assertIn("path: release/OpenADB-3.1.0-unsigned.exe", self.windows)
        self.assertIn(
            "signpath_artifact_id: ${{ steps.upload_signpath_input.outputs.artifact-id }}",
            self.windows,
        )
        signing_job = _job(self.release, "signpath-sign", "verify-release")
        self.assertIn("- wait-for-ci", signing_job)
        self.assertIn("- windows-build", signing_job)
        self.assertIn("environment: signpath-release", signing_job)
        self.assertIn("github-artifact-id: ${{ needs.windows-build.outputs.signpath_artifact_id }}", signing_job)
        self.assertIn("artifact-ids: ${{ needs.windows-build.outputs.signpath_artifact_id }}", signing_job)
        self.assertIn("$files.Count -ne 1", signing_job)
        self.assertIn("OpenADB-3.1.0-unsigned.exe", signing_job)
        self.assertNotIn("contents: write", signing_job)

    def test_configuration_is_explicit_and_partial_setup_fails(self) -> None:
        for name in (
            "SIGNPATH_ENABLED",
            "SIGNPATH_ORGANIZATION_ID",
            "SIGNPATH_PROJECT_SLUG",
            "SIGNPATH_SIGNING_POLICY_SLUG",
            "SIGNPATH_ARTIFACT_CONFIGURATION_SLUG",
            "SIGNPATH_CERTIFICATE_SHA256",
            "SIGNPATH_CERTIFICATE_SUBJECT",
            "SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA",
            "SIGNPATH_RELEASED_FORM_ACCEPTED_TAG",
        ):
            self.assertIn(name, self.release)
            self.assertIn(name, self.setup)
        self.assertIn("SIGNPATH_ENABLED must be absent, false, or true", self.release)
        self.assertIn(
            "SIGNPATH_IDEMPOTENCY_CONFIRMED must be absent, false, or true",
            self.release,
        )
        self.assertIn(
            "SIGNPATH_ENABLED=true requires documented SignPath submission idempotency",
            self.release,
        )
        self.assertIn("is missing or malformed", self.release)
        self.assertIn("Use the exact value issued by SignPath", self.release)
        self.assertIn("SIGNPATH_IDEMPOTENCY_CONFIRMED=true", self.setup)
        self.assertIn("server-side deduplication", self.setup)
        self.assertIn("setting it to zero disables the HTTP timeout", self.setup)
        self.assertIn("SIGNPATH_ENABLED=true", self.setup)
        self.assertIn("Activate last", self.setup)

    def test_windows_build_cannot_use_an_unreviewed_upx_from_path(self) -> None:
        spec = (ROOT / "OpenADB.spec").read_text(encoding="utf-8")
        self.assertIn("upx=False", spec)
        self.assertNotIn("upx=True", spec)

    def test_windows_build_is_architecture_pinned_and_rebuilt_twice(self) -> None:
        self.assertIn("architecture: x64", self.windows)
        self.assertIn('PYTHONHASHSEED: "1"', self.windows)
        self.assertIn("SOURCE_DATE_EPOCH=$sourceDateEpoch", self.windows)
        self.assertIn("git rev-parse HEAD", self.windows)
        self.assertIn("$checkedOutCommit -cne $sourceCommit", self.windows)
        self.assertIn('$env:RUNNER_ARCH -ne "X64"', self.windows)
        self.assertIn("[uint32]::MaxValue", self.windows)
        self.assertIn('platform.machine()', self.windows)
        self.assertIn('struct.calcsize(\'P\') * 8', self.windows)
        self.assertEqual(self.windows.count("python -m PyInstaller --clean"), 2)
        self.assertIn("dist-repro-1", self.windows)
        self.assertIn("dist-repro-2", self.windows)
        self.assertIn(
            "The two clean PyInstaller builds are not byte-for-byte reproducible",
            self.windows,
        )
        for field in (
            "python_hash_seed",
            "source_date_epoch",
            "spec_sha256",
            "runner_image_os",
            "runner_image_version",
            "reproducibility_build_count",
            "reproducibility_sha256",
        ):
            self.assertIn(field, self.windows)
            self.assertIn(field, self.release)
        self.assertIn("OPENADB_REPRODUCIBILITY_BUILD_COUNT=2", self.windows)
        self.assertIn(
            "The canonical PyInstaller output does not exactly match",
            self.windows,
        )
        spec_bytes = (ROOT / "OpenADB.spec").read_bytes()
        self.assertNotIn(b"\r", spec_bytes)
        spec_hash = hashlib.sha256(spec_bytes).hexdigest()
        for workflow in (self.ci, self.windows, self.release):
            pinned_hashes = re.findall(
                r"OPENADB_SPEC_SHA256:\s*([0-9a-f]{64})",
                workflow,
            )
            self.assertTrue(pinned_hashes)
            self.assertEqual(set(pinned_hashes), {spec_hash})

    def test_ci_runs_a_no_secret_reproducible_release_preflight(self) -> None:
        preflight = self.ci[self.ci.index("  release-preflight:") :]
        self.assertIn(
            "name: Windows release preflight without signing secrets",
            preflight,
        )
        self.assertIn("runs-on: windows-2022", preflight)
        self.assertIn('PYTHON_VERSION: "3.12.10"', preflight)
        self.assertIn('PYTHONHASHSEED: "1"', preflight)
        self.assertIn("architecture: x64", preflight)
        self.assertIn("persist-credentials: false", preflight)
        self.assertIn("OPENADB_SPEC_SHA256:", preflight)
        self.assertIn("PLATFORM_TOOLS_ARCHIVE_SHA256:", preflight)
        self.assertIn("SOURCE_DATE_EPOCH=$sourceDateEpoch", preflight)
        self.assertIn('"ANDROID_HOME=$sdkRoot"', preflight)
        self.assertIn('"ANDROID_SDK_ROOT=$sdkRoot"', preflight)
        self.assertEqual(preflight.count("python -m PyInstaller --clean"), 2)
        self.assertIn("dist-preflight-1", preflight)
        self.assertIn("dist-preflight-2", preflight)
        self.assertIn(
            "The two release preflight builds differ byte-for-byte",
            preflight,
        )
        archive_check = preflight[
            preflight.index("$archiveEntries = & python -m PyInstaller") :
        ]
        self.assertIn("PyInstaller.utils.cliutils.archive_viewer", archive_check)
        for entry in (
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "THIRD_PARTY_SOURCES.md",
            "platform-tools/adb.exe",
            "platform-tools/fastboot.exe",
            "platform-tools/AdbWinApi.dll",
            "platform-tools/AdbWinUsbApi.dll",
            "platform-tools/libwinpthread-1.dll",
            "platform-tools/NOTICE.txt",
            "openadb/resources/acbridge/ACBridge-$env:OPENADB_VERSION.apk",
            "PySide6/Qt6Pdf.dll",
            "PySide6/plugins/imageformats/qpdf.dll",
            "libcrypto-3.dll",
            "libssl-3.dll",
            "python312.dll",
        ):
            self.assertIn(entry, archive_check)
        for entry in (
            "PySide6/Qt6WebEngineCore.dll",
            "PySide6/Qt6WebEngineQuick.dll",
            "PySide6/Qt6WebEngineWidgets.dll",
            "PySide6/QtWebEngineCore.pyd",
            "PySide6/QtWebEngineQuick.pyd",
            "PySide6/QtWebEngineWidgets.pyd",
        ):
            self.assertIn(entry, archive_check)
        self.assertIn("contains an unreviewed browser payload", archive_check)
        self.assertNotIn("secrets.", preflight)
        self.assertNotIn("environment:", preflight)
        self.assertNotIn("contents: write", preflight)
        self.assertNotIn("actions/upload-artifact@", preflight)

    def test_release_wait_exceeds_the_ci_preflight_timeout(self) -> None:
        preflight = self.ci[self.ci.index("  release-preflight:") :]
        preflight_timeout = int(
            re.search(r"timeout-minutes:\s*(\d+)", preflight).group(1)
        )
        wait_job = _job(self.release, "wait-for-ci", "signpath-sign")
        wait_deadline = int(re.search(r"AddMinutes\((\d+)\)", wait_job).group(1))
        wait_job_timeout = int(
            re.search(r"timeout-minutes:\s*(\d+)", wait_job).group(1)
        )
        self.assertGreater(wait_deadline, preflight_timeout)
        self.assertGreater(wait_job_timeout, wait_deadline)

    def test_same_run_build_provenance_is_checked_at_every_release_gate(self) -> None:
        verify_job = _job(self.release, "verify-release", "verify-publication")
        independent_job = _job(self.release, "verify-publication", "publish")
        publish_job = self.release[self.release.index("  publish:") :]

        self.assertEqual(verify_job.count("$buildKeys = @("), 2)
        self.assertEqual(independent_job.count("$buildKeys = @("), 1)
        self.assertEqual(publish_job.count("$buildKeys = @("), 1)
        self.assertIn(
            "Final BUILD_STATUS.json contains invalid same-run build provenance",
            verify_job,
        )
        self.assertIn(
            "Fresh-runner BUILD_STATUS.json contains invalid same-run build provenance",
            independent_job,
        )
        self.assertIn(
            "Publisher BUILD_STATUS.json contains invalid same-run build provenance",
            publish_job,
        )
        for job in (verify_job, independent_job, publish_job):
            self.assertIn("$expectedUnsignedBuildHash", job)
            self.assertIn("status.signing.unsigned_sha256", job)
            self.assertIn("reproducibility_build_count", job)
            self.assertIn("OPENADB_SPEC_SHA256", job)

    def test_complete_provenance_and_two_independent_windows_gates_are_required(self) -> None:
        for field in (
            "provider = 'signpath'",
            "signing_request_id",
            "signing_request_web_url",
            "input_artifact_id",
            "result_artifact_id",
            "unsigned_sha256",
            "signed_sha256",
            "certificate_subject",
            "certificate_issuer",
            "certificate_serial",
            "certificate_sha256",
            "timestamp_certificate_sha256",
            "source_commit",
            "source_ref",
            "run_attempt",
        ):
            self.assertIn(field, self.release)
        self.assertGreaterEqual(self.release.count("verify /pa /all /v /tw"), 2)
        self.assertGreaterEqual(
            self.release.count("tools/verify_authenticode_payload.py"), 2
        )
        self.assertGreaterEqual(
            self.release.count("Get-AuthenticodeSignature -LiteralPath"), 2
        )
        self.assertIn("1.3.6.1.5.5.7.3.3", self.release)
        self.assertIn("TimeStamperCertificate", self.release)
        self.assertIn("OriginalFilename -ne 'OpenADB-3.1.0.exe'", self.release)
        self.assertIn("The release tag changed immediately before publication", self.release)

        verify_job = _job(self.release, "verify-release", "verify-publication")
        independent_job = _job(self.release, "verify-publication", "publish")
        publish_job = self.release[self.release.index("  publish:") :]
        self.assertIn("runs-on: windows-latest", verify_job)
        self.assertIn("contents: read", verify_job)
        self.assertNotIn("contents: write", verify_job)
        self.assertIn("runs-on: windows-latest", independent_job)
        self.assertIn("contents: read", independent_job)
        self.assertNotIn("contents: write", independent_job)
        self.assertIn("needs.verify-release.outputs.artifact_id", independent_job)
        self.assertIn(
            "artifact-ids: ${{ needs.verify-release.outputs.artifact_id }}",
            independent_job,
        )
        self.assertIn("fresh-runner PE payload verification", independent_job)
        self.assertIn("fresh-runner pre-publication signtool gate", independent_job)
        self.assertIn("app.signpath.io", independent_job)
        self.assertIn("[Guid]::TryParseExact", independent_job)
        self.assertIn("$requestUri.Query", independent_job)
        self.assertIn("$requestUri.Fragment", independent_job)
        self.assertNotIn("gh release create", verify_job)
        self.assertNotIn("gh release create", independent_job)
        self.assertIn("contents: write", publish_job)
        self.assertIn("VERIFIED_ARTIFACT_DIGEST", publish_job)
        self.assertIn("VERIFIED_ARTIFACT_DIGEST", independent_job)
        self.assertIn("read-only verifier downloaded an artifact", independent_job)
        self.assertIn(
            "VERIFIED_ARTIFACT_ID: ${{ needs.verify-release.outputs.artifact_id }}",
            publish_job,
        )
        self.assertNotIn("upload_approved_release", independent_job)
        self.assertNotIn("actions/upload-artifact@", independent_job)
        self.assertIn(
            "artifact_digest: ${{ steps.upload_verified_release.outputs.artifact-digest }}",
            verify_job,
        )
        self.assertIn("path: publication-bundle", verify_job)
        self.assertGreaterEqual(verify_job.count("Get-ChildItem"), 1)
        self.assertNotRegex(
            verify_job,
            r"Get-ChildItem -LiteralPath (?:(?:'publication-bundle')|\$release)(?![^\r\n]*-Force)",
        )
        self.assertNotRegex(
            independent_job,
            r"Get-ChildItem -LiteralPath \$release(?![^\r\n]*-Force)",
        )
        self.assertNotRegex(
            publish_job,
            r"Get-ChildItem -LiteralPath (?:(?:'verified-bundle(?:/unsigned-evidence)?')|\$release)(?![^\r\n]*-Force)",
        )

    def test_write_token_is_scoped_to_minimal_publisher_steps(self) -> None:
        verify_job = _job(self.release, "verify-release", "verify-publication")
        independent_job = _job(self.release, "verify-publication", "publish")
        publish_job = self.release[self.release.index("  publish:") :]
        self.assertNotIn("contents: write", verify_job)
        self.assertNotIn("contents: write", independent_job)
        self.assertEqual(publish_job.count("contents: write"), 1)
        self.assertEqual(publish_job.count("GH_TOKEN: ${{ github.token }}"), 1)
        self.assertNotIn("uses:", publish_job)
        self.assertEqual(publish_job.count("      - name:"), 1)
        self.assertNotIn("python ", publish_job)
        self.assertNotIn("tools/", publish_job)
        self.assertIn(
            "gh release view $env:RELEASE_TAG --repo $env:GITHUB_REPOSITORY",
            publish_job,
        )
        self.assertIn("'--repo', $env:GITHUB_REPOSITORY", publish_job)
        self.assertIn(
            "Verify the publication artifact origin with read-only GitHub access",
            independent_job,
        )
        self.assertIn(
            "Download, verify, and publish the immutable approved bundle",
            publish_job,
        )
        independent_gate = independent_job[
            independent_job.index(
                "      - name: Independently verify the immutable publication bundle"
            ) :
        ]
        self.assertNotIn("GH_TOKEN", independent_gate)

    def test_artifact_configuration_is_one_restricted_pe_in_a_zip(self) -> None:
        configuration = ROOT / ".signpath" / "artifact-configuration.xml"
        tree = ET.parse(configuration)
        namespace = {"sp": "http://signpath.io/artifact-configuration/v1"}
        root = tree.getroot()
        self.assertEqual(
            root.tag,
            "{http://signpath.io/artifact-configuration/v1}artifact-configuration",
        )
        parameters = root.findall("sp:parameters/sp:parameter", namespace)
        self.assertEqual(len(parameters), 1)
        self.assertEqual(parameters[0].attrib, {"name": "version", "required": "true"})
        zip_files = root.findall("sp:zip-file", namespace)
        self.assertEqual(len(zip_files), 1)
        pe_files = zip_files[0].findall("sp:pe-file", namespace)
        self.assertEqual(len(pe_files), 1)
        self.assertEqual(pe_files[0].attrib["path"], "OpenADB-${version}-unsigned.exe")
        self.assertEqual(pe_files[0].attrib["product-name"], "OpenADB")
        self.assertEqual(pe_files[0].attrib["original-filename"], "OpenADB-${version}.exe")
        sign = pe_files[0].find("sp:authenticode-sign", namespace)
        self.assertIsNotNone(sign)
        self.assertEqual(sign.attrib["hash-algorithm"], "sha256")

    def test_public_docs_do_not_claim_pending_application_is_active(self) -> None:
        self.assertIn("application is pending", self.setup)
        self.assertIn("No current OpenADB artifact is SignPath-signed", self.setup)
        self.assertIn("repository-side SignPath workflow is implemented but disabled", self.readme)
        self.assertIn("The former\nPFX signing path has been removed", self.readme)
        self.assertIn("containing exactly one clean unsigned OpenADB Windows EXE", self.privacy)
        self.assertIn("does not add runtime SignPath communication or telemetry", self.privacy)


if __name__ == "__main__":
    unittest.main()
