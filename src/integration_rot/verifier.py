"""Executed verification.

`verify_fix` drafts a fix, applies it to an isolated copy of the target repo,
and runs the generated contract tests with pytest — the migration is not just
drafted, it is *executed*. The console output doubles as a migration-evidence
bundle: what was changed, which contract tests ran, and whether they passed.

The target repo itself is never modified; all work happens in a temp dir.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .fixer import draft_fix, write_fix

# Never copied into the verification sandbox.
_IGNORE = {".git", "__pycache__", ".pytest_cache", ".venv", "venv",
           "node_modules", ".eggs", ".tox"}


@dataclass
class VerifyResult:
    entry_id: str
    file: str
    changes_applied: list = field(default_factory=list)  # written rel paths
    tests: list = field(default_factory=list)            # contract test paths
    tests_run: int = 0
    passed: bool = False
    output: str = ""          # captured pytest output (tail)
    notes: list = field(default_factory=list)


def _copy_repo(src: str | Path, dest: str | Path) -> None:
    src, dest = Path(src), Path(dest)
    for item in src.iterdir():
        if item.name in _IGNORE or item.name.endswith(".egg-info"):
            continue
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target,
                            ignore=shutil.ignore_patterns(*_IGNORE))
        else:
            shutil.copy2(item, target)


def verify_fix(repo_path: str | Path, entry_id: str, rel_path: str,
               timeout: int = 180, **kwargs) -> VerifyResult:
    """Draft a fix, apply it to a sandbox copy, run contract tests.

    Returns a VerifyResult. Raises KeyError if no fixer exists for entry_id.
    `kwargs` are forwarded to the fixer (module, func_name, --param values).
    """
    draft = draft_fix(entry_id, repo_path, rel_path, **kwargs)
    res = VerifyResult(entry_id=entry_id, file=rel_path, notes=list(draft.notes))
    if draft.empty():
        res.output = "nothing to verify: the fixer found no matching code"
        return res
    with tempfile.TemporaryDirectory(prefix="integration-rot-verify-") as tmp:
        _copy_repo(repo_path, tmp)
        res.changes_applied = write_fix(tmp, draft)
        res.tests = [t for t, _ in draft.tests]
        env = dict(os.environ)
        env["PYTHONPATH"] = tmp + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *res.tests, "-q",
             "--no-header", "-p", "no:cacheprovider"],
            cwd=tmp, env=env, capture_output=True, text=True,
            timeout=timeout,
        )
        res.tests_run = len(res.tests)
        res.passed = proc.returncode == 0
        res.output = (proc.stdout + proc.stderr)[-4000:]
    return res


def print_evidence(res: VerifyResult) -> None:
    """Render the migration-evidence bundle to the console."""
    print(f"\n=== evidence: {res.entry_id} on {res.file} ===")
    if not res.changes_applied:
        print("No changes — nothing was verified.")
        print(f"  output: {res.output}")
        return
    print(f"Files changed in sandbox: {', '.join(res.changes_applied)}")
    print(f"Contract tests run: {res.tests_run} "
          f"({'PASS' if res.passed else 'FAIL'})")
    print("--- pytest output ---")
    print(res.output.rstrip())
    print("--- end pytest output ---")
    if res.notes:
        print("Reviewer notes:")
        for n in res.notes:
            print(f"  - {n}")
    print()
