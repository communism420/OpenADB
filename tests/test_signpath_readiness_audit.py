from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.audit_signpath_readiness import (
    ACTION_PIN,
    AuditReport,
    Finding,
    LIVE_RULESET_PINS,
    SIGNPATH_ACTION_SHA,
    Status,
    _api_ruleset_matches,
    _exact_default_branch_ci_is_green,
    _exact_default_branch_ci_runs_path,
    _signpath_protected_values_valid,
    _signpath_value_formats,
    audit,
    collect_workflow_actions,
)


ROOT = Path(__file__).resolve().parents[1]


class SignPathReadinessAuditTests(unittest.TestCase):
    def test_security_policy_and_codeowners_cover_release_boundary(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        codeowners = (ROOT / ".github" / "CODEOWNERS").read_text(
            encoding="utf-8"
        )
        self.assertIn("[Security policy](SECURITY.md)", readme)
        self.assertIn(
            "https://github.com/communism420/OpenADB/security/advisories/new",
            security,
        )
        self.assertNotIn("mailto:", security.casefold())
        for protected_path in (
            "/.github/actions-allowlist.json @communism420",
            "/.github/workflows/ @communism420",
            "/.github/rulesets/ @communism420",
            "/.signpath/ @communism420",
            "/OpenADB.spec @communism420",
            "/openadb/version.py @communism420",
            "/tools/audit_signpath_readiness.py @communism420",
            "/SECURITY.md @communism420",
        ):
            self.assertIn(protected_path, codeowners)

    def test_workflow_actions_exactly_match_checked_in_allowlist(self) -> None:
        allowlist = json.loads(
            (ROOT / ".github" / "actions-allowlist.json").read_text(
                encoding="utf-8"
            )
        )
        configured = allowlist["patterns_allowed"]
        self.assertFalse(allowlist["github_owned_allowed"])
        self.assertFalse(allowlist["verified_allowed"])
        self.assertEqual(len(configured), len(set(configured)))
        self.assertTrue(all(ACTION_PIN.fullmatch(value) for value in configured))
        self.assertEqual(
            collect_workflow_actions(ROOT / ".github" / "workflows"),
            set(configured),
        )

    def test_signpath_protected_value_formats_are_fail_closed(self) -> None:
        values = {
            "SIGNPATH_ORGANIZATION_ID": "12345678-1234-4234-9234-123456789abc",
            "SIGNPATH_PROJECT_SLUG": "openadb",
            "SIGNPATH_SIGNING_POLICY_SLUG": "release-policy",
            "SIGNPATH_ARTIFACT_CONFIGURATION_SLUG": "windows-exe",
            "SIGNPATH_CERTIFICATE_SHA256": "a" * 64,
            "SIGNPATH_CERTIFICATE_SUBJECT": "CN=OpenADB",
        }
        self.assertTrue(_signpath_value_formats(values))
        self.assertTrue(
            _signpath_value_formats(
                {**values, "SIGNPATH_PROJECT_SLUG": "OpenADB.Release_1"}
            )
        )
        for name, invalid in (
            ("SIGNPATH_ORGANIZATION_ID", "not-a-uuid"),
            ("SIGNPATH_PROJECT_SLUG", "OpenADB With Spaces"),
            ("SIGNPATH_CERTIFICATE_SHA256", "A" * 64),
            ("SIGNPATH_CERTIFICATE_SUBJECT", "CN=OpenADB\nInjected"),
            ("SIGNPATH_CERTIFICATE_SUBJECT", "x" * 513),
        ):
            with self.subTest(name=name):
                candidate = {**values, name: invalid}
                self.assertFalse(_signpath_value_formats(candidate))

        self.assertTrue(
            _signpath_protected_values_valid(values, {"SIGNPATH_API_TOKEN"})
        )
        self.assertFalse(
            _signpath_protected_values_valid(
                values,
                {"SIGNPATH_API_TOKEN", "UNEXPECTED_SECRET"},
            )
        )
        self.assertFalse(
            _signpath_protected_values_valid(
                {**values, "UNEXPECTED_VARIABLE": "value"},
                {"SIGNPATH_API_TOKEN"},
            )
        )

    def test_reviewed_signpath_action_pin_is_an_exact_lowercase_sha(self) -> None:
        self.assertRegex(SIGNPATH_ACTION_SHA, r"^[0-9a-f]{40}$")
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"SIGNPATH_ACTION_SHA: {SIGNPATH_ACTION_SHA}", workflow)
        self.assertIn(
            "${{ vars.SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA }}",
            workflow,
        )

    def test_exact_head_ci_requires_a_successful_default_branch_push(self) -> None:
        head = "a" * 40
        failed_push = {
            "head_sha": head,
            "head_branch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "failure",
        }
        successful_pull_request = {
            "head_sha": head,
            "head_branch": "main",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
        }
        self.assertFalse(
            _exact_default_branch_ci_is_green(
                [successful_pull_request, failed_push],
                head=head,
                default_branch="main",
            )
        )
        self.assertTrue(
            _exact_default_branch_ci_is_green(
                [
                    successful_pull_request,
                    {
                        **failed_push,
                        "conclusion": "success",
                    },
                ],
                head=head,
                default_branch="main",
            )
        )
        self.assertFalse(
            _exact_default_branch_ci_is_green(
                [{**failed_push, "conclusion": "success"}],
                head=head,
                default_branch="develop",
            )
        )
        self.assertEqual(
            _exact_default_branch_ci_runs_path(
                "owner/repository",
                head=head,
                default_branch="release/main",
            ),
            "repos/owner/repository/actions/workflows/ci.yml/runs"
            f"?event=push&branch=release%2Fmain&head_sha={head}"
            "&status=success&per_page=100",
        )

    def test_report_exit_codes_distinguish_failures_and_pending_gates(self) -> None:
        passing = AuditReport(
            "preapproval",
            "owner/repository",
            (Finding("pass", Status.PASS, "ok"),),
        )
        pending = AuditReport(
            "preapproval",
            "owner/repository",
            (
                Finding("pass", Status.PASS, "ok"),
                Finding("pending", Status.PENDING, "waiting"),
            ),
        )
        failing = AuditReport(
            "preapproval",
            "owner/repository",
            (
                Finding("pending", Status.PENDING, "waiting"),
                Finding("fail", Status.FAIL, "broken"),
            ),
        )
        self.assertEqual(passing.exit_code, 0)
        self.assertEqual(pending.exit_code, 2)
        self.assertEqual(failing.exit_code, 1)

    def test_json_report_contains_statuses_but_no_secret_values(self) -> None:
        report = AuditReport(
            "preapproval",
            "owner/repository",
            (Finding("github.signpath-protected-values", Status.PASS, "absent"),),
        )
        payload = report.as_json()
        rendered = json.dumps(payload)
        self.assertEqual(payload["schema"], "openadb.signpath-readiness.v1")
        self.assertEqual(payload["counts"]["pass"], 1)
        self.assertNotIn("SIGNPATH_API_TOKEN=", rendered)

    def test_collect_workflow_actions_ignores_local_reusable_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test.yml").write_text(
                "jobs:\n"
                "  local:\n"
                "    uses: ./.github/workflows/local.yml\n"
                "  remote:\n"
                "    steps:\n"
                "      - uses: actions/checkout@" + "1" * 40 + " # pinned\n",
                encoding="utf-8",
            )
            self.assertEqual(
                collect_workflow_actions(root),
                {"actions/checkout@" + "1" * 40},
            )

    def test_offline_output_cannot_authorize_activation_or_active_state(self) -> None:
        for mode in ("activation", "active"):
            with self.subTest(mode=mode):
                report = audit(
                    root=ROOT,
                    repository="communism420/OpenADB",
                    mode=mode,
                    offline=True,
                )
                live_state = next(
                    finding
                    for finding in report.findings
                    if finding.check == "github.live-state"
                )
                self.assertIs(live_state.status, Status.PENDING)
                self.assertEqual(report.exit_code, 2)

    def test_live_ruleset_match_requires_identity_revision_and_source(self) -> None:
        expected = json.loads(
            (
                ROOT
                / ".github"
                / "rulesets"
                / "protected-main-history.json"
            ).read_text(encoding="utf-8")
        )
        pin = LIVE_RULESET_PINS["protected-main-history.json"]
        actual = {
            **expected,
            "id": pin["id"],
            "updated_at": pin["updated_at"],
            "source_type": "Repository",
            "source": "communism420/OpenADB",
        }
        self.assertTrue(
            _api_ruleset_matches(
                expected,
                actual,
                repository="communism420/OpenADB",
                expected_id=pin["id"],
                expected_updated_at=pin["updated_at"],
            )
        )
        equivalent_offset = {
            **actual,
            "updated_at": "2026-08-28T20:33:17.786+03:00",
        }
        self.assertTrue(
            _api_ruleset_matches(
                expected,
                equivalent_offset,
                repository="communism420/OpenADB",
                expected_id=pin["id"],
                expected_updated_at=pin["updated_at"],
            )
        )
        for key, wrong in (
            ("id", pin["id"] + 1),
            ("updated_at", "2026-01-01T00:00:00Z"),
            ("source", "fork/OpenADB"),
        ):
            with self.subTest(key=key):
                self.assertFalse(
                    _api_ruleset_matches(
                        expected,
                        {**actual, key: wrong},
                        repository="communism420/OpenADB",
                        expected_id=pin["id"],
                        expected_updated_at=pin["updated_at"],
                    )
                )


if __name__ == "__main__":
    unittest.main()
