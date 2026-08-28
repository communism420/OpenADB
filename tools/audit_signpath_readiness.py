"""Read-only OpenADB SignPath and GitHub release-readiness audit.

The audit deliberately never reads secret values. In ``preapproval`` mode it
requires SignPath activation flags to remain false and protected SignPath
values to remain absent. In ``activation`` mode it verifies a fully provisioned
but still disabled configuration. ``active`` mode verifies the final enabled
state. Non-secret protected values are validated only by format.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOSITORY = "communism420/OpenADB"
SIGNPATH_ENVIRONMENT = "signpath-release"
SIGNPATH_ACTION_SHA = "c92b958760219087e01f8d67a1669ed57afe2627"
ACTION_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
LOWER_SHA1 = re.compile(r"^[0-9a-f]{40}$")
LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)

REQUIRED_SIGNPATH_VARIABLES = {
    "SIGNPATH_ORGANIZATION_ID",
    "SIGNPATH_PROJECT_SLUG",
    "SIGNPATH_SIGNING_POLICY_SLUG",
    "SIGNPATH_ARTIFACT_CONFIGURATION_SLUG",
    "SIGNPATH_CERTIFICATE_SHA256",
    "SIGNPATH_CERTIFICATE_SUBJECT",
}
REQUIRED_SIGNPATH_SECRETS = {"SIGNPATH_API_TOKEN"}
RELEASED_FORM_ACCEPTED_TAG_VARIABLE = "SIGNPATH_RELEASED_FORM_ACCEPTED_TAG"
RELEASE_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
LIVE_RULESET_PINS = {
    "immutable-release-tags.json": {
        "id": 21743660,
        "updated_at": "2026-08-28T15:36:49.876Z",
    },
    "protected-main-history.json": {
        "id": 21750700,
        "updated_at": "2026-08-28T17:33:17.786Z",
    },
}
LOCAL_RULESET_EXPECTATIONS = {
    "immutable-release-tags.json": {
        "name": "Immutable OpenADB release tags",
        "target": "tag",
        "rules": {"update", "deletion", "non_fast_forward"},
        "include": {
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
        },
    },
    "protected-main-history.json": {
        "name": "Protected OpenADB main history",
        "target": "branch",
        "rules": {"deletion", "non_fast_forward"},
        "include": {"~DEFAULT_BRANCH"},
    },
}
REQUIRED_REPOSITORY_FILES = {
    ".github/actions-allowlist.json",
    ".github/rulesets/immutable-release-tags.json",
    ".github/rulesets/protected-main-history.json",
    ".signpath/artifact-configuration.xml",
    "LICENSE",
    "PRIVACY.md",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "THIRD_PARTY_SOURCES.md",
    "docs/RELEASE_PROCESS.md",
    "docs/SIGNPATH_SETUP.md",
}


class Status(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    PENDING = "pending"
    FAIL = "fail"


@dataclass(frozen=True)
class Finding:
    check: str
    status: Status
    message: str


@dataclass(frozen=True)
class AuditReport:
    mode: str
    repository: str
    findings: tuple[Finding, ...]

    @property
    def exit_code(self) -> int:
        if any(finding.status is Status.FAIL for finding in self.findings):
            return 1
        if any(finding.status is Status.PENDING for finding in self.findings):
            return 2
        return 0

    def as_json(self) -> dict[str, Any]:
        counts = {
            status.value: sum(
                finding.status is status for finding in self.findings
            )
            for status in Status
        }
        return {
            "schema": "openadb.signpath-readiness.v1",
            "mode": self.mode,
            "repository": self.repository,
            "exit_code": self.exit_code,
            "counts": counts,
            "findings": [
                {**asdict(finding), "status": finding.status.value}
                for finding in self.findings
            ],
        }


class AuditError(RuntimeError):
    """Raised when a read-only audit input cannot be obtained safely."""


def _run_text(command: Sequence[str], *, cwd: Path = ROOT) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout).split())
        raise AuditError(f"Read-only command failed: {' '.join(command)}: {detail}")
    return result.stdout


class GitHubReader:
    """Minimal read-only GitHub CLI wrapper."""

    def __init__(self, repository: str) -> None:
        self.repository = repository

    def api(self, path: str) -> Any:
        output = _run_text(["gh", "api", path])
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise AuditError(f"GitHub returned malformed JSON for {path}") from exc


def _finding(check: str, passed: bool, success: str, failure: str) -> Finding:
    return Finding(check, Status.PASS if passed else Status.FAIL, success if passed else failure)


def collect_workflow_actions(workflows_root: Path) -> set[str]:
    actions: set[str] = set()
    for path in sorted(workflows_root.glob("*.y*ml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
            if match is None or match.group(1).startswith("./"):
                continue
            actions.add(match.group(1))
    return actions


def _local_repository_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    missing_files = sorted(
        relative for relative in REQUIRED_REPOSITORY_FILES if not (root / relative).is_file()
    )
    findings.append(
        _finding(
            "repository.required-files",
            not missing_files,
            "All policy, legal, SignPath, and release-readiness files are present.",
            f"Required repository files are missing: {', '.join(missing_files)}",
        )
    )

    allowlist_path = root / ".github" / "actions-allowlist.json"
    workflows_root = root / ".github" / "workflows"
    allowlist_valid = False
    allowlist_detail = "The checked-in Actions allowlist is missing."
    if allowlist_path.is_file():
        try:
            allowlist = json.loads(allowlist_path.read_text(encoding="utf-8"))
            configured = allowlist.get("patterns_allowed", [])
            actual = collect_workflow_actions(workflows_root)
            allowlist_valid = (
                allowlist.get("github_owned_allowed") is False
                and allowlist.get("verified_allowed") is False
                and isinstance(configured, list)
                and len(configured) == len(set(configured))
                and all(isinstance(value, str) and ACTION_PIN.fullmatch(value) for value in configured)
                and actual == set(configured)
            )
            allowlist_detail = (
                "Every external workflow action exactly matches the reviewed SHA allowlist."
                if allowlist_valid
                else "The checked-in Actions allowlist does not exactly match every external workflow action."
            )
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            allowlist_detail = (
                f"The checked-in Actions allowlist is unreadable: {type(exc).__name__}"
            )
    findings.append(
        Finding(
            "repository.actions-allowlist",
            Status.PASS if allowlist_valid else Status.FAIL,
            allowlist_detail,
        )
    )

    artifact_path = root / ".signpath" / "artifact-configuration.xml"
    artifact_valid = False
    artifact_detail = "The SignPath Artifact Configuration is missing."
    if artifact_path.is_file():
        try:
            document = ET.parse(artifact_path)
            root_element = document.getroot()
            namespace = {"sp": root_element.tag.partition("}")[0].lstrip("{")}
            pe_files = root_element.findall(".//sp:pe-file", namespace)
            signatures = root_element.findall(".//sp:authenticode-sign", namespace)
            artifact_valid = (
                len(pe_files) == 1
                and pe_files[0].get("path") == "OpenADB-${version}-unsigned.exe"
                and len(signatures) == 1
                and signatures[0].get("hash-algorithm") == "sha256"
            )
            artifact_detail = (
                "Artifact Configuration accepts exactly the versioned unsigned EXE and SHA-256 Authenticode."
                if artifact_valid
                else "Artifact Configuration no longer has the reviewed one-EXE SHA-256 contract."
            )
        except (ET.ParseError, OSError) as exc:
            artifact_detail = f"Artifact Configuration is unreadable: {type(exc).__name__}"
    findings.append(
        Finding(
            "repository.artifact-configuration",
            Status.PASS if artifact_valid else Status.FAIL,
            artifact_detail,
        )
    )

    for filename, expected in LOCAL_RULESET_EXPECTATIONS.items():
        path = root / ".github" / "rulesets" / filename
        valid = False
        detail = f"The checked-in ruleset {filename} is missing or malformed."
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                rules = {item.get("type") for item in payload.get("rules", [])}
                conditions = payload.get("conditions", {}).get("ref_name", {})
                valid = (
                    payload.get("name") == expected["name"]
                    and payload.get("target") == expected["target"]
                    and payload.get("enforcement") == "active"
                    and payload.get("bypass_actors") == []
                    and conditions.get("exclude") == []
                    and set(conditions.get("include", [])) == expected["include"]
                    and len(conditions.get("include", []))
                    == len(expected["include"])
                    and rules == expected["rules"]
                    and len(payload.get("rules", [])) == len(expected["rules"])
                )
                detail = (
                    f"The checked-in {expected['target']} ruleset exactly matches its reviewed policy."
                    if valid
                    else f"The checked-in {expected['target']} ruleset drifted from its reviewed policy."
                )
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        findings.append(
            Finding(
                f"repository.ruleset.{expected['target']}",
                Status.PASS if valid else Status.FAIL,
                detail,
            )
        )
    return findings


def _api_ruleset_matches(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    repository: str,
    expected_id: int,
    expected_updated_at: str,
) -> bool:
    try:
        actual_revision = datetime.fromisoformat(
            str(actual.get("updated_at", "")).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        expected_revision = datetime.fromisoformat(
            expected_updated_at.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except ValueError:
        return False
    return (
        actual.get("id") == expected_id
        and actual_revision == expected_revision
        and actual.get("name") == expected.get("name")
        and actual.get("target") == expected.get("target")
        and actual.get("enforcement") == "active"
        and actual.get("source_type") == "Repository"
        and actual.get("source") == repository
        and actual.get("bypass_actors") == []
        and actual.get("conditions") == expected.get("conditions")
        and actual.get("rules") == expected.get("rules")
    )


def _named_values(payload: dict[str, Any], key: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in payload.get(key, []):
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str):
            values[name] = value if isinstance(value, str) else ""
    return values


def _signpath_value_formats(values: dict[str, str]) -> bool:
    subject = values.get("SIGNPATH_CERTIFICATE_SUBJECT", "")
    organization_id = values.get("SIGNPATH_ORGANIZATION_ID", "")
    return (
        UUID.fullmatch(organization_id.lower()) is not None
        and organization_id != "00000000-0000-0000-0000-000000000000"
        and SLUG.fullmatch(values.get("SIGNPATH_PROJECT_SLUG", "")) is not None
        and SLUG.fullmatch(values.get("SIGNPATH_SIGNING_POLICY_SLUG", "")) is not None
        and SLUG.fullmatch(values.get("SIGNPATH_ARTIFACT_CONFIGURATION_SLUG", ""))
        is not None
        and LOWER_SHA256.fullmatch(values.get("SIGNPATH_CERTIFICATE_SHA256", ""))
        is not None
        and bool(subject.strip())
        and len(subject.strip()) <= 512
        and "\n" not in subject
        and "\r" not in subject
    )


def _signpath_protected_values_valid(
    variables: dict[str, str],
    secret_names: set[str],
) -> bool:
    return (
        REQUIRED_SIGNPATH_VARIABLES == variables.keys()
        and REQUIRED_SIGNPATH_SECRETS == secret_names
        and _signpath_value_formats(variables)
    )


def _exact_default_branch_ci_is_green(
    workflow_runs: Iterable[dict[str, Any]],
    *,
    head: str,
    default_branch: str,
) -> bool:
    """Require a successful push CI run for the exact live default-branch head."""

    return any(
        str(run.get("head_sha", "")).lower() == head
        and run.get("head_branch") == default_branch
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        for run in workflow_runs
    )


def _exact_default_branch_ci_runs_path(
    repository: str,
    *,
    head: str,
    default_branch: str,
) -> str:
    return (
        f"repos/{repository}/actions/workflows/ci.yml/runs"
        f"?event=push&branch={quote(default_branch, safe='')}"
        f"&head_sha={quote(head, safe='')}&status=success&per_page=100"
    )


def _live_findings(
    root: Path,
    repository: str,
    mode: str,
    api: Callable[[str], Any],
) -> list[Finding]:
    findings: list[Finding] = []
    head = _run_text(["git", "rev-parse", "HEAD"], cwd=root).strip().lower()
    repository_state = api(f"repos/{repository}")
    default_branch = repository_state.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch.strip():
        raise AuditError("GitHub did not return the repository default branch")
    default_branch = default_branch.strip()
    branch_state = api(f"repos/{repository}/branches/{quote(default_branch, safe='')}")
    live_default_head = str(branch_state.get("commit", {}).get("sha", "")).lower()
    origin_default = _run_text(
        ["git", "rev-parse", f"refs/remotes/origin/{default_branch}"], cwd=root
    ).strip().lower()
    status = _run_text(["git", "status", "--porcelain"], cwd=root)
    findings.append(
        _finding(
            "git.clean",
            not status.strip(),
            "The local worktree is clean.",
            "The local worktree has uncommitted changes; audit results are not a release snapshot.",
        )
    )
    findings.append(
        _finding(
            "git.live-default-head",
            LOWER_SHA1.fullmatch(head) is not None
            and head == live_default_head
            and head == origin_default,
            f"Local HEAD exactly matches GitHub's live {default_branch} head and origin tracking ref.",
            f"Local HEAD, GitHub's live {default_branch} head, and the origin tracking ref do not identify one source snapshot.",
        )
    )

    permissions = api(f"repos/{repository}/actions/permissions")
    findings.append(
        _finding(
            "github.actions-policy",
            permissions.get("enabled") is True
            and permissions.get("allowed_actions") == "selected"
            and permissions.get("sha_pinning_required") is True,
            "GitHub Actions is enabled only for selected, full-SHA-pinned actions.",
            "GitHub Actions selected/full-SHA enforcement is not active.",
        )
    )

    expected_allowlist = json.loads(
        (root / ".github" / "actions-allowlist.json").read_text(encoding="utf-8")
    )
    live_allowlist = api(f"repos/{repository}/actions/permissions/selected-actions")
    allowlist_matches = (
        live_allowlist.get("github_owned_allowed")
        == expected_allowlist["github_owned_allowed"]
        and live_allowlist.get("verified_allowed")
        == expected_allowlist["verified_allowed"]
        and set(live_allowlist.get("patterns_allowed", []))
        == set(expected_allowlist["patterns_allowed"])
        and len(live_allowlist.get("patterns_allowed", []))
        == len(expected_allowlist["patterns_allowed"])
    )
    findings.append(
        _finding(
            "github.actions-allowlist",
            allowlist_matches,
            "The live Actions allowlist exactly matches the checked-in SHA allowlist.",
            "The live Actions allowlist drifted from the checked-in SHA allowlist.",
        )
    )

    fork_policy = api(
        f"repos/{repository}/actions/permissions/fork-pr-contributor-approval"
    )

    security = repository_state.get("security_and_analysis", {})
    automated_fixes = api(f"repos/{repository}/automated-security-fixes")
    private_reporting = api(f"repos/{repository}/private-vulnerability-reporting")
    findings.append(
        _finding(
            "github.dependabot-security",
            security.get("dependabot_security_updates", {}).get("status")
            == "enabled"
            and automated_fixes.get("enabled") is True
            and automated_fixes.get("paused") is False,
            "Dependabot security updates and automated security fixes are enabled.",
            "Dependabot security updates or automated security fixes are disabled or paused.",
        )
    )
    findings.append(
        _finding(
            "github.private-vulnerability-reporting",
            private_reporting.get("enabled") is True,
            "GitHub private vulnerability reporting is enabled.",
            "GitHub private vulnerability reporting is disabled.",
        )
    )
    extended_secret_scanning = (
        security.get("secret_scanning_non_provider_patterns", {}).get("status")
        == "enabled"
        and security.get("secret_scanning_validity_checks", {}).get("status")
        == "enabled"
    )
    findings.append(
        Finding(
            "github.extended-secret-scanning",
            Status.PASS if extended_secret_scanning else Status.WARNING,
            (
                "Secret scanning validity checks and non-provider patterns are enabled."
                if extended_secret_scanning
                else "GitHub did not enable validity checks or non-provider secret patterns for this repository; core secret scanning and push protection remain enabled."
            ),
        )
    )
    findings.append(
        _finding(
            "github.fork-approval",
            fork_policy.get("approval_policy") == "all_external_contributors",
            "Every external fork contributor requires workflow approval.",
            "External fork workflow approval is weaker than the reviewed policy.",
        )
    )

    immutable = api(f"repos/{repository}/immutable-releases")
    findings.append(
        _finding(
            "github.release-immutability",
            immutable.get("enabled") is True,
            "GitHub Release Immutability is enabled for future releases.",
            "GitHub Release Immutability is disabled.",
        )
    )

    live_rulesets = api(f"repos/{repository}/rulesets?per_page=100")
    for filename in ("immutable-release-tags.json", "protected-main-history.json"):
        expected = json.loads(
            (root / ".github" / "rulesets" / filename).read_text(encoding="utf-8")
        )
        matches = [item for item in live_rulesets if item.get("name") == expected["name"]]
        matched = False
        if len(matches) == 1 and isinstance(matches[0].get("id"), int):
            details = api(f"repos/{repository}/rulesets/{matches[0]['id']}")
            pin = LIVE_RULESET_PINS[filename]
            matched = _api_ruleset_matches(
                expected,
                details,
                repository=repository,
                expected_id=pin["id"],
                expected_updated_at=pin["updated_at"],
            )
        findings.append(
            _finding(
                f"github.ruleset.{expected['target']}",
                matched,
                f"The live {expected['target']} ruleset exactly matches its no-bypass policy.",
                f"The live {expected['target']} ruleset is missing, duplicated, or drifted.",
            )
        )

    environment = api(f"repos/{repository}/environments/{SIGNPATH_ENVIRONMENT}")
    reviewers = [
        rule
        for rule in environment.get("protection_rules", [])
        if rule.get("type") == "required_reviewers" and rule.get("reviewers")
    ]
    findings.append(
        _finding(
            "github.signpath-environment-review",
            len(reviewers) == 1
            and environment.get("deployment_branch_policy", {}).get(
                "custom_branch_policies"
            )
            is True,
            "The SignPath environment has a required reviewer and custom tag policy.",
            "The SignPath environment reviewer or custom tag policy is missing.",
        )
    )
    findings.append(
        _finding(
            "github.signpath-environment-admin-bypass",
            environment.get("can_admins_bypass") is False,
            "Administrators cannot bypass the SignPath environment gate.",
            "Administrators can still bypass the SignPath environment gate; disable this in the GitHub UI.",
        )
    )
    policies = api(
        f"repos/{repository}/environments/{SIGNPATH_ENVIRONMENT}/deployment-branch-policies"
    )
    policy_pairs = {
        (item.get("name"), item.get("type"))
        for item in policies.get("branch_policies", [])
    }
    findings.append(
        _finding(
            "github.signpath-tag-policy",
            policy_pairs == {("v*", "tag")},
            "Only v* tags may enter the SignPath environment.",
            "The SignPath environment tag allowlist is not exactly v*.",
        )
    )

    variables = _named_values(api(f"repos/{repository}/actions/variables?per_page=100"), "variables")
    expected_enabled, expected_confirmed = {
        "preapproval": ("false", "false"),
        "activation": ("false", "true"),
        "active": ("true", "true"),
    }[mode]
    activation_flags_valid = (
        variables.get("SIGNPATH_ENABLED") == expected_enabled
        and variables.get("SIGNPATH_IDEMPOTENCY_CONFIRMED")
        == expected_confirmed
    )
    findings.append(
        _finding(
            "github.signpath-activation-flags",
            activation_flags_valid,
            "SignPath activation flags match the required "
            f"{mode} state (enabled={expected_enabled}, "
            f"idempotency_confirmed={expected_confirmed}).",
            "SignPath activation flags do not match the required "
            f"{mode} state (enabled={expected_enabled}, "
            f"idempotency_confirmed={expected_confirmed}).",
        )
    )
    reviewed_action_sha = variables.get(
        "SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA"
    )
    if mode == "preapproval":
        reviewed_action_valid = reviewed_action_sha is None
        reviewed_action_message = (
            "No action SHA is marked as idempotency-reviewed before SignPath approval."
            if reviewed_action_valid
            else "A SignPath action SHA was marked as reviewed before approval."
        )
    else:
        reviewed_action_valid = (
            reviewed_action_sha == SIGNPATH_ACTION_SHA
            and LOWER_SHA1.fullmatch(reviewed_action_sha or "") is not None
        )
        reviewed_action_message = (
            "The idempotency review is bound to the exact checked-in SignPath action SHA."
            if reviewed_action_valid
            else "The idempotency review is not bound to the exact checked-in SignPath action SHA."
        )
    findings.append(
        Finding(
            "github.signpath-reviewed-action-sha",
            Status.PASS if reviewed_action_valid else Status.FAIL,
            reviewed_action_message,
        )
    )

    environment_variables = _named_values(
        api(
            f"repos/{repository}/environments/{SIGNPATH_ENVIRONMENT}/variables?per_page=100"
        ),
        "variables",
    )
    environment_secrets_payload = api(
        f"repos/{repository}/environments/{SIGNPATH_ENVIRONMENT}/secrets?per_page=100"
    )
    environment_secret_names = {
        item.get("name")
        for item in environment_secrets_payload.get("secrets", [])
        if isinstance(item.get("name"), str)
    }
    if mode == "preapproval":
        protected_values_valid = (
            not (REQUIRED_SIGNPATH_VARIABLES & environment_variables.keys())
            and not (REQUIRED_SIGNPATH_SECRETS & environment_secret_names)
        )
        protected_message = (
            "SignPath-issued variables and token remain intentionally absent while approval is pending."
            if protected_values_valid
            else "SignPath protected values appeared before activation review."
        )
    else:
        protected_values_valid = _signpath_protected_values_valid(
            environment_variables,
            environment_secret_names,
        )
        protected_message = (
            "The exact SignPath protected value names exist and non-secret formats validate."
            if protected_values_valid
            else "SignPath protected value names are incomplete or contain unexpected entries, or a non-secret format is invalid."
        )
    findings.append(
        Finding(
            "github.signpath-protected-values",
            Status.PASS if protected_values_valid else Status.FAIL,
            protected_message,
        )
    )

    workflow_runs = api(
        _exact_default_branch_ci_runs_path(
            repository,
            head=head,
            default_branch=default_branch,
        )
    ).get("workflow_runs", [])
    ci_green = _exact_default_branch_ci_is_green(
        workflow_runs,
        head=head,
        default_branch=default_branch,
    )
    findings.append(
        _finding(
            "github.exact-head-ci",
            ci_green,
            f"Windows CI is green for exact HEAD {head}.",
            f"No successful Windows CI run exists for exact HEAD {head}.",
        )
    )

    accepted_release_tag = variables.get(RELEASED_FORM_ACCEPTED_TAG_VARIABLE)
    if mode == "preapproval":
        eligibility_status = (
            Status.PENDING if accepted_release_tag is None else Status.FAIL
        )
        eligibility_message = (
            "SignPath's written released-form eligibility decision is still pending."
            if accepted_release_tag is None
            else "A released-form acceptance tag was recorded before SignPath approval."
        )
    elif RELEASE_TAG.fullmatch(accepted_release_tag or "") is None:
        eligibility_status = Status.FAIL
        eligibility_message = (
            "SIGNPATH_RELEASED_FORM_ACCEPTED_TAG is missing or is not an exact version tag."
        )
    else:
        accepted_release = api(
            f"repos/{repository}/releases/tags/{quote(accepted_release_tag, safe='')}"
        )
        accepted_release_valid = (
            accepted_release.get("tag_name") == accepted_release_tag
            and accepted_release.get("draft") is False
            and isinstance(accepted_release.get("published_at"), str)
            and bool(accepted_release.get("published_at"))
        )
        eligibility_status = (
            Status.PASS if accepted_release_valid else Status.FAIL
        )
        eligibility_message = (
            f"The written SignPath released-form decision is bound to public release {accepted_release_tag}."
            if accepted_release_valid
            else "The released-form acceptance tag does not identify an existing public GitHub release."
        )
    findings.append(
        Finding(
            "github.signpath-released-form-decision",
            eligibility_status,
            eligibility_message,
        )
    )
    return findings


def audit(
    *,
    root: Path,
    repository: str,
    mode: str,
    offline: bool,
    reader: GitHubReader | None = None,
) -> AuditReport:
    findings = _local_repository_findings(root)
    if offline:
        live_skip_status = Status.WARNING if mode == "preapproval" else Status.PENDING
        findings.append(
            Finding(
                "github.live-state",
                live_skip_status,
                (
                    "Live GitHub checks were intentionally skipped."
                    if mode == "preapproval"
                    else f"Live GitHub checks are mandatory before {mode}; offline output cannot authorize this transition."
                ),
            )
        )
    else:
        github = reader or GitHubReader(repository)
        try:
            findings.extend(
                _live_findings(root, repository, mode, github.api)
            )
        except (AuditError, KeyError, TypeError, ValueError, OSError) as exc:
            findings.append(
                Finding(
                    "github.live-state",
                    Status.FAIL,
                    f"Live GitHub audit could not complete safely: {exc}",
                )
            )
    return AuditReport(mode, repository, tuple(findings))


def _write_report(path: Path, report: AuditReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.as_json(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _print_report(report: AuditReport) -> None:
    for finding in report.findings:
        print(f"[{finding.status.value.upper():7}] {finding.check}: {finding.message}")
    counts = report.as_json()["counts"]
    print(
        "Summary: "
        + ", ".join(f"{name}={count}" for name, count in counts.items())
        + f", exit_code={report.exit_code}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("preapproval", "activation", "active"),
        default="preapproval",
        help="Expected SignPath lifecycle state.",
    )
    parser.add_argument(
        "--repository",
        default=DEFAULT_REPOSITORY,
        help="GitHub owner/repository to audit.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Check only committed repository policy and configuration.",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        help="Optional sanitized JSON report path.",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    report = audit(
        root=ROOT,
        repository=arguments.repository,
        mode=arguments.mode,
        offline=arguments.offline,
    )
    _print_report(report)
    if arguments.json_report is not None:
        _write_report(arguments.json_report, report)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
