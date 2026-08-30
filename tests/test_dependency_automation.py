from __future__ import annotations

import unittest
from pathlib import Path

from tools.verify_release_dependencies import load_active_requirements


ROOT = Path(__file__).resolve().parents[1]


class DependencyAutomationTests(unittest.TestCase):
    def test_qt_for_python_pins_are_atomic(self) -> None:
        requirements = load_active_requirements(ROOT / "requirements.txt")
        versions = {
            requirements[name].version
            for name in (
                "pyside6",
                "pyside6-essentials",
                "pyside6-addons",
                "shiboken6",
            )
        }
        self.assertEqual(len(versions), 1)

    def test_dependabot_groups_coupled_python_dependencies(self) -> None:
        config = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        qt_patterns = (
            "          - pyside6\n"
            "          - pyside6-essentials\n"
            "          - pyside6-addons\n"
            "          - shiboken6\n"
        )
        build_patterns = (
            "          - setuptools\n"
            "          - wheel\n"
            "          - packaging\n"
        )
        expected_groups = {
            "pyside6-stack": ("version-updates", qt_patterns),
            "python-build-toolchain": ("version-updates", build_patterns),
            "pyside6-stack-security": ("security-updates", qt_patterns),
            "python-build-toolchain-security": ("security-updates", build_patterns),
        }
        for group, (applies_to, patterns) in expected_groups.items():
            expected_block = (
                f"      {group}:\n"
                f"        applies-to: {applies_to}\n"
                "        patterns:\n"
                f"{patterns}"
            )
            self.assertIn(expected_block, config)

    def test_pull_requests_do_not_duplicate_push_ci(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        trigger = workflow[workflow.index("on:\n") : workflow.index("permissions:\n")]
        self.assertEqual(
            trigger,
            "on:\n  push:\n    branches:\n      - main\n  pull_request:\n\n",
        )


if __name__ == "__main__":
    unittest.main()
