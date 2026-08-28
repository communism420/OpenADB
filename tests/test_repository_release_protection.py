from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RULESET_NAME = "Immutable OpenADB release tags"
MAIN_RULESET_NAME = "Protected OpenADB main history"
PROTECTED_REFS = {
    "refs/tags/v*",
    "refs/tags/0.9.0beta",
    "refs/tags/1.0.0",
    "refs/tags/1.1.0",
    "refs/tags/2.0.0",
    "refs/tags/2.0.1",
    "refs/tags/3.0.0",
    "refs/tags/3.0.1",
    "refs/tags/3.0.2",
    "refs/tags/3.0.3",
}


def _job(workflow: str, name: str, next_name: str | None = None) -> str:
    start = workflow.index(f"  {name}:")
    if next_name is None:
        return workflow[start:]
    end = workflow.index(f"  {next_name}:", start + len(name) + 3)
    return workflow[start:end]


class RepositoryReleaseProtectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ruleset_path = (
            ROOT / ".github" / "rulesets" / "immutable-release-tags.json"
        )
        cls.ruleset = json.loads(cls.ruleset_path.read_text(encoding="utf-8"))
        cls.main_ruleset = json.loads(
            (
                ROOT / ".github" / "rulesets" / "protected-main-history.json"
            ).read_text(encoding="utf-8")
        )
        cls.actions_allowlist = json.loads(
            (ROOT / ".github" / "actions-allowlist.json").read_text(
                encoding="utf-8"
            )
        )
        cls.release = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        cls.setup = (ROOT / "docs" / "SIGNPATH_SETUP.md").read_text(
            encoding="utf-8"
        )
        cls.release_process = (ROOT / "docs" / "RELEASE_PROCESS.md").read_text(
            encoding="utf-8"
        )

    def test_checked_in_ruleset_is_active_exact_and_has_no_bypass(self) -> None:
        self.assertEqual(self.ruleset["name"], RULESET_NAME)
        self.assertEqual(self.ruleset["target"], "tag")
        self.assertEqual(self.ruleset["enforcement"], "active")
        self.assertEqual(self.ruleset["bypass_actors"], [])
        self.assertEqual(
            set(self.ruleset["conditions"]["ref_name"]["include"]),
            PROTECTED_REFS,
        )
        self.assertEqual(self.ruleset["conditions"]["ref_name"]["exclude"], [])
        rule_types = [rule["type"] for rule in self.ruleset["rules"]]
        self.assertEqual(
            set(rule_types), {"update", "deletion", "non_fast_forward"}
        )
        self.assertEqual(len(rule_types), 3)
        self.assertNotIn("creation", rule_types)

    def test_every_external_action_is_pinned_to_a_full_commit_sha(self) -> None:
        workflows_root = ROOT / ".github" / "workflows"
        workflow_actions: set[str] = set()
        for workflow_path in sorted(workflows_root.glob("*.yml")):
            workflow = workflow_path.read_text(encoding="utf-8")
            for line_number, line in enumerate(workflow.splitlines(), start=1):
                match = re.match(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
                if match is None:
                    continue
                reference = match.group(1)
                if reference.startswith("./"):
                    continue
                workflow_actions.add(reference)
                self.assertRegex(
                    reference,
                    r"^[^@\s]+@[0-9a-fA-F]{40}$",
                    f"Unpinned action at {workflow_path}:{line_number}",
                )
        self.assertFalse(self.actions_allowlist["github_owned_allowed"])
        self.assertFalse(self.actions_allowlist["verified_allowed"])
        configured_actions = self.actions_allowlist["patterns_allowed"]
        self.assertEqual(len(configured_actions), len(set(configured_actions)))
        self.assertEqual(workflow_actions, set(configured_actions))

    def test_default_branch_history_ruleset_is_safe_and_non_disruptive(self) -> None:
        self.assertEqual(self.main_ruleset["name"], MAIN_RULESET_NAME)
        self.assertEqual(self.main_ruleset["target"], "branch")
        self.assertEqual(self.main_ruleset["enforcement"], "active")
        self.assertEqual(self.main_ruleset["bypass_actors"], [])
        self.assertEqual(
            self.main_ruleset["conditions"]["ref_name"],
            {"include": ["~DEFAULT_BRANCH"], "exclude": []},
        )
        rule_types = [rule["type"] for rule in self.main_ruleset["rules"]]
        self.assertEqual(rule_types, ["deletion", "non_fast_forward"])
        self.assertNotIn("update", rule_types)
        self.assertNotIn("creation", rule_types)

    def test_release_checks_remote_ruleset_at_every_security_boundary(self) -> None:
        prepare = _job(self.release, "prepare", "acbridge-release")
        signing = _job(self.release, "signpath-sign", "verify-release")
        publish = _job(self.release, "publish")
        for boundary in (prepare, signing, publish):
            with self.subTest(boundary=boundary[:80]):
                self.assertIn("function Assert-ReleaseTagProtection", boundary)
                self.assertIn("Assert-ReleaseTagProtection", boundary)
                self.assertIn(
                    'rulesets/$($matches[0].id)',
                    boundary,
                )
                self.assertIn("$ruleset.enforcement -ne 'active'", boundary)
                self.assertIn("$actualUpdatedAt.UtcTicks", boundary)
                self.assertIn("$bypassActorsVisible -and", boundary)
                self.assertIn("@($ruleset.bypass_actors).Count -ne 0", boundary)
                self.assertIn("$actualRules.Count -ne $expectedRules.Count", boundary)
                self.assertIn(
                    "drifted from the reviewed no-bypass policy",
                    boundary,
                )
                self.assertIn("function Assert-DefaultBranchProtection", boundary)
                self.assertIn("Assert-DefaultBranchProtection", boundary)
                branch_start = boundary.index(
                    "function Assert-DefaultBranchProtection"
                )
                branch_call = boundary.index(
                    "\n          Assert-DefaultBranchProtection",
                    branch_start,
                )
                branch_guard = boundary[branch_start:branch_call]
                self.assertIn("$env:DEFAULT_BRANCH_RULESET_NAME", branch_guard)
                self.assertIn("$env:DEFAULT_BRANCH_RULESET_ID", branch_guard)
                self.assertIn("$env:DEFAULT_BRANCH_RULESET_UPDATED_AT", branch_guard)
                self.assertIn("$ruleset.target -ne 'branch'", branch_guard)
                self.assertIn("$bypassActorsVisible -and", branch_guard)
                self.assertIn("@($ruleset.bypass_actors).Count -ne 0", branch_guard)
                self.assertIn("@($ruleset.conditions.ref_name.exclude).Count", branch_guard)
                self.assertIn("$expectedRules = @('deletion', 'non_fast_forward')", branch_guard)
        self.assertEqual(
            self.release.count("function Assert-ReleaseTagProtection"), 3
        )
        self.assertEqual(
            self.release.count("function Assert-DefaultBranchProtection"), 3
        )
        self.assertIn(f"RELEASE_TAG_RULESET_NAME: {RULESET_NAME}", self.release)
        self.assertIn('RELEASE_TAG_RULESET_ID: "21743660"', self.release)
        self.assertIn(
            'RELEASE_TAG_RULESET_UPDATED_AT: "2026-08-28T15:36:49.876Z"',
            self.release,
        )
        workflow_refs_match = re.search(
            r"RELEASE_TAG_RULESET_REFS:\s*>-\s*\n\s*(\[[^\n]+\])",
            self.release,
        )
        self.assertIsNotNone(workflow_refs_match)
        assert workflow_refs_match is not None
        self.assertEqual(
            json.loads(workflow_refs_match.group(1)),
            self.ruleset["conditions"]["ref_name"]["include"],
        )
        for protected_ref in PROTECTED_REFS:
            self.assertIn(protected_ref, self.release)
        self.assertIn(f"DEFAULT_BRANCH_RULESET_NAME: {MAIN_RULESET_NAME}", self.release)
        self.assertIn('DEFAULT_BRANCH_RULESET_ID: "21750700"', self.release)
        self.assertIn(
            'DEFAULT_BRANCH_RULESET_UPDATED_AT: "2026-08-28T17:33:17.786Z"',
            self.release,
        )
        self.assertIn('["~DEFAULT_BRANCH"]', self.release)

    def test_release_source_remains_on_default_branch_at_security_boundaries(self) -> None:
        prepare = _job(self.release, "prepare", "acbridge-release")
        signing = _job(self.release, "signpath-sign", "verify-release")
        publish = _job(self.release, "publish")
        for boundary in (prepare, signing, publish):
            with self.subTest(boundary=boundary[:80]):
                self.assertIn("default_branch", boundary)
                self.assertIn("/branches/$encodedDefaultBranch", boundary)
                self.assertIn("defaultHeadSha", boundary)
                self.assertIn("merge_base_commit.sha", boundary)
                self.assertIn("base_commit.sha", boundary)
                self.assertIn("@('ahead', 'identical')", boundary)
                self.assertIn("compare/${targetSha}...${defaultHeadSha}", boundary)
                self.assertNotIn("head_commit", boundary)
        self.assertIn(
            "The selected release tag does not resolve to the workflow source commit.",
            prepare,
        )

    def test_publisher_requires_existing_tag_and_future_release_immutability(self) -> None:
        publish = _job(self.release, "publish")
        self.assertIn("'--verify-tag'", publish)
        self.assertIn("/releases/tags/$env:RELEASE_TAG", publish)
        self.assertIn("$createOutput = @(& gh @arguments 2>&1)", publish)
        self.assertIn("function Stop-UnverifiedStableRelease", publish)
        self.assertIn("$published.immutable -isnot [bool]", publish)
        self.assertIn("$withdrawn.immutable -isnot [bool]", publish)
        self.assertIn("$published.name -eq $title", publish)
        self.assertIn("$published.body -ceq $expectedReleaseBody", publish)
        self.assertIn("$publishedAssetMap.Count -ne $expectedAssetMap.Count", publish)
        self.assertIn("Published asset identity mismatch", publish)
        self.assertIn("verified as withdrawn to draft", publish)
        self.assertIn("treat the published release as a security incident", publish)
        self.assertIn("independently verified as stable and immutable", publish)
        self.assertIn(
            "gh release edit $env:RELEASE_TAG --repo $env:GITHUB_REPOSITORY --draft",
            publish,
        )
        self.assertLess(
            publish.index("$createOutput = @(& gh @arguments 2>&1)"),
            publish.index("$publishedText = ''"),
        )
        self.assertNotRegex(
            self.release,
            r"(?im)^\s*(?:git|gh)\s+.*(?:--force|-f\b).*refs/tags/",
        )
        self.assertNotIn("git update-ref -d refs/tags/", self.release)

    def test_maintainer_docs_describe_irreversible_tag_and_release_policy(self) -> None:
        for document in (self.setup, self.release_process):
            self.assertIn(RULESET_NAME, document)
            self.assertIn("immutable-release-tags.json", document)
            self.assertIn("no bypass", document.lower())
            self.assertIn("GitHub Release Immutability", document)
        self.assertRegex(self.release_process, r"does not\s+restrict creation")
        self.assertIn("never delete it", self.release_process.lower())
        self.assertIn("only to future releases", self.setup)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is required")
    def test_stable_publisher_reconciles_ambiguous_remote_states(self) -> None:
        marker = "          $ErrorActionPreference = 'Continue'\n          $createOutput"
        start = self.release.index(marker)
        reconciliation = textwrap.dedent(self.release[start:])
        mock = r"""
            Set-StrictMode -Version Latest
            $ErrorActionPreference = 'Stop'
            $env:STABLE = 'true'
            $env:RELEASE_TAG = 'v-test'
            $env:GITHUB_REPOSITORY = 'communism420/OpenADB'
            $arguments = @('release', 'create', $env:RELEASE_TAG)
            $title = 'OpenADB test'
            $expectedReleaseBody = "notes`n"
            $expectedAssetMap = @{
              'asset.bin' = [pscustomobject]@{
                size = [long]4
                digest = 'sha256:aaaaaaaa'
              }
            }
            $script:withdrawalAttempted = $false
            $script:draft = $false

            function global:Start-Sleep {
              param([int]$Seconds)
            }

            function global:gh {
              param(
                [Parameter(ValueFromRemainingArguments = $true)]
                [object[]]$Arguments
              )
              $command = @($Arguments | ForEach-Object { [string]$_ })
              if ($command[0] -eq 'release' -and $command[1] -eq 'create') {
                $global:LASTEXITCODE = [int]$env:MOCK_CREATE_EXIT
                Write-Output 'mock create response'
                return
              }
              if ($command[0] -eq 'release' -and $command[1] -eq 'edit') {
                $script:withdrawalAttempted = $true
                if ($env:MOCK_EDIT_EFFECTIVE -eq 'true') {
                  $script:draft = $true
                }
                $global:LASTEXITCODE = [int]$env:MOCK_EDIT_EXIT
                Write-Output 'mock withdrawal response'
                return
              }
              if ($command[0] -ne 'api') {
                throw "Unexpected mock gh call: $($command -join ' ')"
              }
              if ($env:MOCK_STATE -eq 'not-found') {
                $global:LASTEXITCODE = 1
                Write-Output 'HTTP 404: Not Found'
                return
              }
              if ($env:MOCK_STATE -eq 'unreadable' -and
                  -not $script:withdrawalAttempted) {
                $global:LASTEXITCODE = 1
                Write-Output 'HTTP 503: Service Unavailable'
                return
              }
              $global:LASTEXITCODE = 0
              if ($script:draft) {
                Write-Output '{"tag_name":"v-test","draft":true,"prerelease":false,"immutable":false}'
                return
              }
              if ($env:MOCK_STATE -eq 'immutable') {
                Write-Output '{"tag_name":"v-test","name":"OpenADB test","body":"notes\n","draft":false,"prerelease":false,"immutable":true,"assets":[{"name":"asset.bin","size":4,"digest":"sha256:aaaaaaaa","state":"uploaded"}]}'
                return
              }
              if ($env:MOCK_STATE -eq 'immutable-wrong-asset') {
                Write-Output '{"tag_name":"v-test","name":"OpenADB test","body":"notes\n","draft":false,"prerelease":false,"immutable":true,"assets":[{"name":"asset.bin","size":4,"digest":"sha256:bbbbbbbb","state":"uploaded"}]}'
                return
              }
              if ($env:MOCK_STATE -eq 'immutable-extra-asset') {
                Write-Output '{"tag_name":"v-test","name":"OpenADB test","body":"notes\n","draft":false,"prerelease":false,"immutable":true,"assets":[{"name":"asset.bin","size":4,"digest":"sha256:aaaaaaaa","state":"uploaded"},{"name":"extra.bin","size":1,"digest":"sha256:cccccccc","state":"uploaded"}]}'
                return
              }
              if ($env:MOCK_STATE -eq 'missing-property') {
                Write-Output '{"tag_name":"v-test","name":"OpenADB test","body":"notes\n","draft":false,"prerelease":false,"assets":[{"name":"asset.bin","size":4,"digest":"sha256:aaaaaaaa","state":"uploaded"}]}'
                return
              }
              Write-Output '{"tag_name":"v-test","name":"OpenADB test","body":"notes\n","draft":false,"prerelease":false,"immutable":false,"assets":[{"name":"asset.bin","size":4,"digest":"sha256:aaaaaaaa","state":"uploaded"}]}'
            }
        """
        scenarios = (
            ("immutable", 1, 1, False, 0, "independently verified"),
            ("immutable-wrong-asset", 1, 1, False, 1, "security incident"),
            ("immutable-extra-asset", 1, 1, False, 1, "security incident"),
            ("not-found", 1, 1, False, 1, "no release exists"),
            ("mutable", 0, 0, True, 1, "verified as withdrawn to draft"),
            ("mutable", 0, 0, False, 1, "security incident"),
            (
                "missing-property",
                0,
                0,
                True,
                1,
                "verified as withdrawn to draft",
            ),
            ("unreadable", 0, 0, True, 1, "verified as withdrawn to draft"),
        )
        pwsh = shutil.which("pwsh")
        assert pwsh is not None
        with tempfile.TemporaryDirectory() as directory:
            script_path = Path(directory) / "publisher-reconciliation.ps1"
            script_path.write_text(
                textwrap.dedent(mock) + "\n" + reconciliation,
                encoding="utf-8",
            )
            for (
                state,
                create_exit,
                edit_exit,
                edit_effective,
                expected_exit,
                expected_text,
            ) in scenarios:
                with self.subTest(state=state, effective=edit_effective):
                    environment = {
                        "MOCK_STATE": state,
                        "MOCK_CREATE_EXIT": str(create_exit),
                        "MOCK_EDIT_EXIT": str(edit_exit),
                        "MOCK_EDIT_EFFECTIVE": str(edit_effective).lower(),
                    }
                    result = subprocess.run(
                        [pwsh, "-NoProfile", "-NonInteractive", "-File", script_path],
                        capture_output=True,
                        check=False,
                        encoding="utf-8",
                        errors="replace",
                        env={**dict(os.environ), **environment},
                    )
                    combined = result.stdout + result.stderr
                    ansi_escape = re.compile(
                        r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"
                    )
                    normalized = " ".join(
                        ansi_escape.sub("", combined).replace("|", " ").split()
                    )
                    self.assertEqual(
                        result.returncode,
                        expected_exit,
                        msg=combined,
                    )
                    self.assertIn(expected_text, normalized)


if __name__ == "__main__":
    unittest.main()
