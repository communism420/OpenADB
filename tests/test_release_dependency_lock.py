from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tools.verify_release_dependencies import (
    BOOTSTRAP_OPTIONS,
    BOOTSTRAP_VERSIONS,
    BUILD_OPTIONS,
    load_active_requirements,
    load_hash_lock,
    validate_bootstrap_environment,
    validate_release_environment,
)


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_LOCK = ROOT / "requirements-bootstrap-win-py312.lock"
BUILD_LOCK = ROOT / "requirements-build-win-py312.lock"
REQUIREMENTS = ROOT / "requirements-build.txt"
WINDOWS_WORKFLOW = ROOT / ".github" / "workflows" / "windows-build.yml"


def _locks_and_requirements():
    bootstrap = load_hash_lock(
        BOOTSTRAP_LOCK,
        expected_options=BOOTSTRAP_OPTIONS,
    )
    build = load_hash_lock(BUILD_LOCK, expected_options=BUILD_OPTIONS)
    requirements = load_active_requirements(REQUIREMENTS)
    return bootstrap, build, requirements


class ReleaseDependencyLockTests(unittest.TestCase):
    def test_hash_locks_match_reviewed_windows_requirements(self) -> None:
        bootstrap, build, requirements = _locks_and_requirements()
        validate_bootstrap_environment(
            lock=bootstrap,
            installed=dict(BOOTSTRAP_VERSIONS),
            python_version="3.12.10",
        )
        installed = {name: value.version for name, value in build.items()}
        validate_release_environment(
            lock=build,
            bootstrap_lock=bootstrap,
            requirements=requirements,
            installed=installed,
            python_version="3.12.10",
        )

    def test_lock_rejects_missing_or_non_sha256_hashes(self) -> None:
        cases = (
            "example==1.0\n",
            "example==1.0 --hash=sha256:ABCDEF\n",
            f"example==1.0 --hash=sha256:{'a' * 64} --hash=sha256:{'b' * 64}\n",
        )
        for content in cases:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "bad.lock"
                path.write_text(
                    "--only-binary=:all:\n" + content,
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    load_hash_lock(path, expected_options=BOOTSTRAP_OPTIONS)

    def test_lock_rejects_missing_unexpected_or_duplicate_options(self) -> None:
        requirement = f"example==1.0 --hash=sha256:{'a' * 64}\n"
        cases = (
            requirement,
            "--unexpected=true\n" + requirement,
            "--only-binary=:all:\n--only-binary=:all:\n" + requirement,
        )
        for content in cases:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "bad-options.lock"
                path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "option contract"):
                    load_hash_lock(path, expected_options=BOOTSTRAP_OPTIONS)

    def test_bootstrap_gate_rejects_extra_or_wrong_build_tool(self) -> None:
        bootstrap, _build, _requirements = _locks_and_requirements()
        installed = dict(BOOTSTRAP_VERSIONS)
        installed["unexpected-package"] = "1.0"
        with self.assertRaisesRegex(ValueError, "unexpected-package"):
            validate_bootstrap_environment(
                lock=bootstrap,
                installed=installed,
                python_version="3.12.10",
            )

        drifted_lock = dict(bootstrap)
        drifted_lock["wheel"] = replace(drifted_lock["wheel"], version="0.0")
        with self.assertRaisesRegex(ValueError, "reviewed build tools"):
            validate_bootstrap_environment(
                lock=drifted_lock,
                installed=dict(BOOTSTRAP_VERSIONS),
                python_version="3.12.10",
            )

    def test_environment_gate_rejects_extra_distribution(self) -> None:
        bootstrap, build, requirements = _locks_and_requirements()
        installed = {name: value.version for name, value in build.items()}
        installed["unexpected-package"] = "1.0"
        with self.assertRaisesRegex(ValueError, "unexpected-package"):
            validate_release_environment(
                lock=build,
                bootstrap_lock=bootstrap,
                requirements=requirements,
                installed=installed,
                python_version="3.12.10",
            )

    def test_environment_gate_rejects_wrong_python_or_distribution_version(self) -> None:
        bootstrap, build, requirements = _locks_and_requirements()
        installed = {name: value.version for name, value in build.items()}
        with self.assertRaisesRegex(ValueError, "CPython 3.12.10"):
            validate_release_environment(
                lock=build,
                bootstrap_lock=bootstrap,
                requirements=requirements,
                installed=installed,
                python_version="3.12.9",
            )

        installed["pillow"] = "0.0"
        with self.assertRaisesRegex(ValueError, "pillow"):
            validate_release_environment(
                lock=build,
                bootstrap_lock=bootstrap,
                requirements=requirements,
                installed=installed,
                python_version="3.12.10",
            )

    def test_environment_gate_rejects_extras_or_bootstrap_artifact_drift(self) -> None:
        bootstrap, build, requirements = _locks_and_requirements()
        installed = {name: value.version for name, value in build.items()}

        without_qrcode_extra = dict(build)
        without_qrcode_extra["qrcode"] = replace(
            without_qrcode_extra["qrcode"],
            extras=frozenset(),
        )
        with self.assertRaisesRegex(ValueError, "qrcode"):
            validate_release_environment(
                lock=without_qrcode_extra,
                bootstrap_lock=bootstrap,
                requirements=requirements,
                installed=installed,
                python_version="3.12.10",
            )

        changed_bootstrap_hash = dict(build)
        changed_bootstrap_hash["wheel"] = replace(
            changed_bootstrap_hash["wheel"],
            artifact_hash="0" * 64,
        )
        with self.assertRaisesRegex(ValueError, "bootstrap artifacts"):
            validate_release_environment(
                lock=changed_bootstrap_hash,
                bootstrap_lock=bootstrap,
                requirements=requirements,
                installed=installed,
                python_version="3.12.10",
            )

    def test_windows_workflow_forces_hash_checks_before_each_gate(self) -> None:
        workflow = WINDOWS_WORKFLOW.read_text(encoding="utf-8")
        install_step = workflow[
            workflow.index("      - name: Install pinned build dependencies") :
            workflow.index("      - name: Validate packaged release metadata")
        ]
        bootstrap_install = install_step.index("-r requirements-bootstrap-win-py312.lock")
        bootstrap_gate = install_step.index("--phase bootstrap")
        build_install = install_step.index("-r requirements-build-win-py312.lock")
        build_gate = install_step.index("--phase build")
        self.assertLess(bootstrap_install, bootstrap_gate)
        self.assertLess(bootstrap_gate, build_install)
        self.assertLess(build_install, build_gate)
        self.assertGreaterEqual(install_step.count("--force-reinstall"), 2)
        self.assertGreaterEqual(install_step.count("--require-hashes"), 2)
        self.assertGreaterEqual(install_step.count("--no-cache-dir"), 2)
        self.assertIn("--no-build-isolation", install_step)
        self.assertLess(
            install_step.index("python -m venv --clear"),
            bootstrap_install,
        )
        self.assertEqual(install_step.count("& $releasePython -m pip install"), 2)
        self.assertEqual(install_step.count("& $releasePython -m pip check"), 2)
        self.assertEqual(
            install_step.count(
                "& $releasePython tools/verify_release_dependencies.py"
            ),
            2,
        )
        self.assertLess(build_gate, install_step.index("$env:GITHUB_PATH"))
        self.assertLess(build_gate, install_step.index("VIRTUAL_ENV=$releaseVenv"))
        self.assertLess(
            build_gate,
            install_step.index("OPENADB_RELEASE_PYTHON=$releasePython"),
        )


if __name__ == "__main__":
    unittest.main()
