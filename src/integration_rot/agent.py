"""Autonomous migration loop: scan -> rank -> draft -> verify -> keep-or-revert.

`integration-rot agent --path ./myrepo` copies the target repo into a temp
working directory and repeatedly:

    1. scans the working copy and ranks findings by risk,
    2. drafts a fix for the top fixable finding,
    3. applies the draft to the working copy,
    4. runs the fixer's generated contract tests in an isolated sandbox,
    5. keeps the fix only if the tests pass, otherwise reverts it and
       records why.

It stops when no findings remain, when no remaining finding has a fixer,
when an iteration makes no progress, or when --max-iterations is hit.

HONESTY NOTES (read before anthropomorphizing this module):
- The default planner is a DETERMINISTIC policy loop: sort by risk
  (critical > high > medium > low), prefer findings a fixer exists for,
  then break ties by entry id. There is no model, no inference, no
  "AI reasoning" on the default path, and none is claimed.
- --planner-endpoint is an EXPERIMENTAL stub: if given an OpenAI-compatible
  chat-completions URL, the agent asks it to order the findings; on ANY
  failure (no network, no key, bad response, unparseable output) it falls
  back to the deterministic planner and logs that it did so. The default
  path works fully offline.
- The user's repo is NEVER modified in place. All work happens on a temp
  copy; the final report includes unified diffs for the user to review
  and apply themselves.
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import __version__
from .analyzer import analyze
from .deprecations import load_db
from .fixer import draft_fix, write_fix
from .scanner import scan_repo

# Files/dirs never copied into the working copy or sandbox.
_IGNORE = {".git", "__pycache__", ".pytest_cache", ".venv", "venv",
           "node_modules", ".eggs", ".tox"}

_RISK_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# evidence lines look like:  app.py:16: `stripe.Charge.create(` (matches stripe-charges-api)
_EVIDENCE_RE = re.compile(r"^([^:]+):(\d+):")


@dataclass
class IterationLog:
    n: int
    entry_id: str
    file: str
    action: str          # kept | reverted | skipped
    detail: str = ""


@dataclass
class AgentReport:
    repo: str
    iterations: int = 0
    fixed: list = field(default_factory=list)      # entry ids kept
    reverted: list = field(default_factory=list)  # (entry_id, reason)
    unfixable: list = field(default_factory=list)  # (entry_id, reason)
    log: list = field(default_factory=list)        # list[IterationLog]
    remaining: list = field(default_factory=list)  # finding dicts left over
    diffs: list = field(default_factory=list)      # unified diffs vs original
    planner: str = "heuristic"

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "version": __version__,
            "iterations": self.iterations,
            "planner": self.planner,
            "fixed": self.fixed,
            "reverted": [{"entry_id": e, "reason": r}
                         for e, r in self.reverted],
            "unfixable": [{"entry_id": e, "reason": r}
                           for e, r in self.unfixable],
            "remaining_findings": self.remaining,
            "log": [{"iteration": l.n, "entry_id": l.entry_id,
                     "file": l.file, "action": l.action,
                     "detail": l.detail} for l in self.log],
            "diffs": self.diffs,
        }


def _copy_repo(src: str | Path, dest: str | Path) -> None:
    src, dest = Path(src), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in _IGNORE or item.name.endswith(".egg-info"):
            continue
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target,
                            ignore=shutil.ignore_patterns(*_IGNORE))
        else:
            shutil.copy2(item, target)


def _finding_location(finding) -> tuple[str | None, int | None]:
    """(repo-relative file, line) for a finding, from its evidence lines."""
    for ev in finding.evidence:
        m = _EVIDENCE_RE.match(ev)
        if m and f"(matches {finding.entry.id})" in ev:
            return m.group(1), int(m.group(2))
    for ev in finding.evidence:  # fall back to any evidenced file
        m = _EVIDENCE_RE.match(ev)
        if m:
            return m.group(1), int(m.group(2))
    return None, None


def _finding_file(finding) -> str | None:
    """Best-effort repo-relative file for a finding, from its evidence lines."""
    rel, _ = _finding_location(finding)
    return rel


def _enclosing_def(source_path: Path, lineno: int | None) -> str | None:
    """Name of the `def` enclosing `lineno` (AST-based). None if unknown."""
    if lineno is None:
        return None
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", lineno)
            if node.lineno <= lineno <= end:
                # deepest enclosing def wins
                if best is None or node.lineno >= best.lineno:
                    best = node
    return best.name if best else None


def _fixer_exists(entry_id: str, repo: str | Path) -> bool:
    """Probe fixer.py's dispatch: KeyError means no fixer ships for the entry.
    Any file-level error means the fixer exists (it just couldn't read the
    probe file) — existence is what we are testing, not draftability."""
    try:
        draft_fix(entry_id, repo, "__integration_rot_probe__")
        return True
    except KeyError:
        return False
    except (FileNotFoundError, OSError, NotADirectoryError):
        return True


def _heuristic_order(findings: list, repo: str | Path) -> list:
    """Deterministic policy: risk first, fixer-available second, id third."""
    def key(f):
        fixer = 0 if _fixer_exists(f.entry.id, repo) else 1
        return (_RISK_ORDER.get(f.risk, 4), fixer, f.entry.id)
    return sorted(findings, key=key)


def _planner_order(findings: list, repo: str | Path, endpoint: str | None,
                   log: list) -> tuple[list, str]:
    """Order findings. Default: deterministic heuristic. If --planner-endpoint
    is given, try it once; on any failure fall back to the heuristic."""
    heuristic = _heuristic_order(findings, repo)
    if not endpoint:
        return heuristic, "heuristic"
    try:
        ids = [f.entry.id for f in findings]
        prompt = ("You are ordering deprecated-API migration work. "
                  f"Finding ids, most urgent first is best: {ids}. "
                  'Reply with ONLY JSON like {"order": ["id1", "id2"]}.')
        req_body = json.dumps({
            "model": "planner",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }).encode()
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/chat/completions",
            data=req_body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        order = json.loads(content[content.index("{"):content.rindex("}") + 1])["order"]
        by_id = {f.entry.id: f for f in findings}
        ordered = [by_id[i] for i in order if i in by_id]
        ordered += [f for f in findings if f.entry.id not in set(order)]
        log.append("planner-endpoint: used remote ordering")
        return ordered, "remote"
    except Exception as e:  # noqa: BLE001 - stub must never break the loop
        log.append(f"planner-endpoint: fallback to heuristic ({e})")
        return heuristic, "heuristic+fallback"


def _run_contract_tests(workdir: Path, draft, timeout: int = 180) -> tuple[bool, str]:
    """Run a draft's generated contract tests in an isolated sandbox copy."""
    with tempfile.TemporaryDirectory(prefix="integration-rot-agent-") as tmp:
        _copy_repo(workdir, tmp)
        for test_path, content in draft.tests:
            full = Path(tmp) / test_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
        if not draft.tests:
            return False, "fixer produced no contract tests; refusing to keep an untested fix"
        env = dict(os.environ)
        env["PYTHONPATH"] = tmp + os.pathsep + env.get("PYTHONPATH", "")
        # Generated tests are UTF-8; force UTF-8 mode in the child so they
        # import cleanly on Windows installs with a non-UTF-8 default locale.
        env.setdefault("PYTHONUTF8", "1")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest",
             *[t for t, _ in draft.tests],
             "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=tmp, env=env, capture_output=True, text=True,
            timeout=timeout)
        out = (proc.stdout + proc.stderr)[-4000:]
        return proc.returncode == 0, out


def _unified_diffs(original: Path, workdir: Path, rel_paths: list[str]) -> list[str]:
    diffs = []
    for rel in sorted(set(rel_paths)):
        old = original / rel
        new = workdir / rel
        old_text = old.read_text(encoding="utf-8", errors="replace").splitlines() if old.exists() else []
        new_text = new.read_text(encoding="utf-8", errors="replace").splitlines() if new.exists() else []
        if old_text != new_text:
            diff = difflib.unified_diff(
                old_text, new_text,
                fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")
            diffs.append("\n".join(diff))
    return diffs


def run_agent(repo: str | Path, max_iterations: int = 5,
              today: date | None = None,
              planner_endpoint: str | None = None,
              verbose: bool = True) -> AgentReport:
    """Run the autonomous migrate loop. Never modifies `repo` in place."""
    repo = Path(repo)
    if not repo.is_dir():
        raise ValueError(f"not a directory: {repo}")
    report = AgentReport(repo=str(repo))
    planner_notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="integration-rot-agent-work-") as tmp:
        workdir = Path(tmp) / "work"
        _copy_repo(repo, workdir)
        all_entries = load_db()
        settled: set[str] = set()  # entry ids fully processed this run
        reported: set[str] = set()  # entry ids already reported as unfixable
        changed_paths: list[str] = []

        for n in range(1, max_iterations + 1):
            scan = scan_repo(workdir)
            active = [e for e in all_entries if e.id not in settled]
            findings = analyze(scan, active, today=today).findings
            if not findings:
                if verbose:
                    print("no actionable findings remain — stopping.")
                break
            # Dependency-level findings (no code location) can't be patched by
            # a file fixer: report once, then leave them out of the loop.
            actionable = [f for f in findings if _finding_file(f)]
            for f in findings:
                if f not in actionable and f.entry.id not in reported:
                    reason = ("dependency-level finding with no deprecated "
                              "code pattern in source — no file for a fixer "
                              "to patch; upgrade the pinned dependency manually")
                    report.unfixable.append((f.entry.id, reason))
                    reported.add(f.entry.id)
            if not actionable:
                if verbose:
                    print("remaining findings have no patchable code — stopping.")
                break
            ordered, planner = _planner_order(actionable, workdir,
                                              planner_endpoint, planner_notes)
            report.planner = planner
            target = ordered[0]
            rel, lineno = _finding_location(target)

            # contract tests need the real (module, func): derive them from
            # the evidence location instead of guessing fixer defaults
            module = Path(rel).with_suffix("").as_posix().replace("/", ".")
            func_name = _enclosing_def(workdir / rel, lineno)

            if verbose:
                print(f"[iter {n}] top finding: {target.entry.id} "
                      f"({target.risk}) in {rel}")

            try:
                kwargs = {"module": module}
                if func_name:
                    kwargs["func_name"] = func_name
                draft = draft_fix(target.entry.id, workdir, rel, **kwargs)
            except KeyError as e:
                reason = f"no fixer ships for this entry ({e})"
                report.unfixable.append((target.entry.id, reason))
                reported.add(target.entry.id)
                settled.add(target.entry.id)
                report.log.append(IterationLog(n, target.entry.id, rel,
                                               "skipped", reason))
                if verbose:
                    print(f"[iter {n}] SKIPPED {target.entry.id}: {reason}")
                continue

            if draft.empty():
                reason = "fixer found no matching code in this file"
                report.log.append(IterationLog(n, target.entry.id, rel,
                                               "skipped", reason))
                settled.add(target.entry.id)
                if verbose:
                    print(f"[iter {n}] SKIPPED {target.entry.id}: {reason}")
                continue

            # apply to the working copy, then prove it in a sandbox
            backups = {}
            for change in draft.changes:
                full = workdir / change.path
                backups[change.path] = full.read_text(encoding="utf-8") if full.exists() else None
            pre_existing_tests = {t for t, _ in draft.tests
                                  if (workdir / t).exists()}
            written = write_fix(workdir, draft)

            passed, output = _run_contract_tests(workdir, draft)
            report.iterations = n
            # A kept fix is settled even if its dependency-level residual still
            # matches: the deprecated *code* is gone, which is what the fixer
            # can do.
            settled.add(target.entry.id)
            if passed:
                report.fixed.append(target.entry.id)
                changed_paths.extend(written)
                report.log.append(IterationLog(
                    n, target.entry.id, rel, "kept",
                    f"contract tests passed; {len(written)} file(s) changed"))
                if verbose:
                    print(f"[iter {n}] KEPT {target.entry.id}: tests passed")
            else:
                for change in draft.changes:
                    full = workdir / change.path
                    if backups[change.path] is None:
                        full.unlink(missing_ok=True)
                    else:
                        full.write_text(backups[change.path], encoding="utf-8")
                for test_path, _ in draft.tests:  # remove drafted tests too
                    if test_path not in pre_existing_tests:
                        (workdir / test_path).unlink(missing_ok=True)
                tail = output.strip().splitlines()
                reason = ("contract tests failed; fix reverted. "
                          f"pytest tail: {tail[-1] if tail else 'no output'}")
                report.reverted.append((target.entry.id, reason))
                report.log.append(IterationLog(n, target.entry.id, rel,
                                               "reverted", reason))
                if verbose:
                    print(f"[iter {n}] REVERTED {target.entry.id}: tests failed")
        else:
            if verbose:
                print(f"hit --max-iterations ({max_iterations}); stopping.")

        # remaining findings, recomputed against the FULL db on the final
        # working copy — this is the honest end state, including residuals.
        # Entries whose code was migrated by a kept fix are flagged: the
        # deprecated *code* is gone, but the old SDK pin can still match the
        # entry's package criteria (a dependency-level residual, not unmigrated
        # code).
        final = analyze(scan_repo(workdir), all_entries, today=today).findings
        report.remaining = [{
            "entry_id": f.entry.id, "vendor": f.entry.vendor,
            "risk": f.risk, "fix_available": f.entry.fix_available,
            "code_migrated": f.entry.id in settled,
        } for f in final]
        report.diffs = _unified_diffs(repo, workdir, changed_paths)
        for note in planner_notes:
            report.log.append(IterationLog(0, "", "", "skipped", note))

    return report


def print_report(report: AgentReport) -> None:
    print(f"\n=== integration-rot agent report ({report.repo}) ===")
    print(f"planner: {report.planner} | iterations: {report.iterations}")
    print(f"fixed ({len(report.fixed)}): "
          f"{', '.join(report.fixed) if report.fixed else 'none'}")
    if report.reverted:
        print(f"reverted ({len(report.reverted)}):")
        for e, r in report.reverted:
            print(f"  - {e}: {r}")
    if report.unfixable:
        print(f"no fixer / no code location ({len(report.unfixable)}):")
        for e, r in report.unfixable:
            print(f"  - {e}: {r}")
    migrated = [f for f in report.remaining if f.get("code_migrated")]
    unaddressed = [f for f in report.remaining if not f.get("code_migrated")]
    if migrated:
        print(f"code migrated — dependency pin still old ({len(migrated)}):")
        for f in migrated:
            print(f"  - {f['entry_id']} [{f['risk']}]")
        print("  (deprecated code was rewritten and contract-tested; the manifest")
        print("   still pins the old SDK — bump the dependency to close this.)")
    if unaddressed:
        print(f"remaining findings ({len(unaddressed)}):")
        for f in unaddressed:
            print(f"  - {f['entry_id']} [{f['risk']}] "
                  f"(fix_available={f['fix_available']})")
    if not report.remaining:
        print("remaining findings: none")
    if report.diffs:
        print("\n--- diffs vs your repo (NOT applied; review and apply manually) ---")
        for d in report.diffs:
            print(d)
    if report.fixed:
        print(f"\nKept {len(report.fixed)} fix(es), proven by contract tests, in a "
              f"working copy — diffs above.")
    elif not report.reverted and not report.unfixable:
        print("\nNo fixes kept.")
    print("Your repo was not modified. To apply a draft, run")
    print("`integration-rot fix --entry <id> --file <file> --apply <repo>` "
          "per finding.")


def cmd_agent(args) -> int:
    try:
        report = run_agent(args.path,
                           max_iterations=args.max_iterations,
                           today=(date.fromisoformat(args.today)
                                  if args.today else None),
                           planner_endpoint=args.planner_endpoint,
                           verbose=args.format == "console")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print_report(report)
    # Exit semantics mirror `check`: 0 when no critical/high risk remains,
    # 1 when critical/high findings are left unfixable, 2 on usage errors.
    return 0 if not any(f["risk"] in ("critical", "high")
                        for f in report.remaining) else 1
