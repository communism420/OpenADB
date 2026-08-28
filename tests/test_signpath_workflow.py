from __future__ import annotations

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
