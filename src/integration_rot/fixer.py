"""Fix drafter.

For deprecation entries flagged `fix_available`, generates a concrete code
patch (unified diff) plus a contract-test sketch. MVP implements the
Stripe Charges API -> PaymentIntents migration; other vendors are registered
as stubs for v2.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileChange:
    path: str          # repo-relative path
    diff: str          # unified diff
    new_content: str   # full new file content


@dataclass
class FixDraft:
    entry_id: str
    changes: list = field(default_factory=list)      # list[FileChange]
    tests: list = field(default_factory=list)        # list[(path, content)]
    notes: list = field(default_factory=list)        # human-readable caveats

    def empty(self) -> bool:
        return not self.changes


# ---------------------------------------------------------------------------
# Stripe: stripe.Charge.create(...) -> stripe.PaymentIntent.create(...)
# ---------------------------------------------------------------------------

_CHARGE_CALL_RE = re.compile(r"stripe\.Charge\.create\s*\(")


def _extract_balanced_args(source: str, open_paren_idx: int) -> tuple[str, int]:
    """Return (args_text, index_after_close) for the call starting at open_paren_idx."""
    depth = 0
    in_str: str | None = None
    escaped = False
    i = open_paren_idx
    while i < len(source):
        ch = source[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
        else:
            if ch in ("'", '"'):
                in_str = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return source[open_paren_idx + 1:i], i + 1
        i += 1
    raise ValueError("unbalanced parentheses in stripe.Charge.create call")


def _split_top_level_kwargs(arg_text: str) -> list[str]:
    parts, depth, in_str, escaped, cur = [], 0, None, False, ""
    for ch in arg_text:
        if in_str:
            cur += ch
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
            cur += ch
        elif ch in "([{":
            depth += 1
            cur += ch
        elif ch in ")]}":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _migrate_charge_kwargs(arg_text: str, indent: str = "    ") -> tuple[str, list[str]]:
    """Rewrite Charge.create kwargs to PaymentIntent.create kwargs.

    Returns (new_arg_text, notes). Mapping: source -> payment_method,
    plus confirm=True and SCA-safe automatic_payment_methods.
    """
    notes = []
    new_kwargs = []
    seen_payment_method = False
    for kw in _split_top_level_kwargs(arg_text):
        m = re.match(r"(\w+)\s*=\s*(.*)$", kw, re.S)
        if not m:
            new_kwargs.append(kw)  # positional or odd -> keep, flag below
            notes.append(f"kept non-keyword argument as-is, verify: `{kw[:60]}`")
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "source":
            new_kwargs.append(f"payment_method={val}")
            seen_payment_method = True
        elif key in ("amount", "currency", "description", "metadata",
                     "receipt_email", "customer", "statement_descriptor"):
            new_kwargs.append(kw)
        elif key == "capture":
            notes.append("`capture` has no PaymentIntent equivalent — "
                         "use capture_method='manual' if you relied on uncaptured auth")
            new_kwargs.append("capture_method='manual'")
        else:
            new_kwargs.append(kw)
            notes.append(f"carried over unmapped kwarg `{key}` — verify against "
                         "PaymentIntent params")
    if not seen_payment_method:
        notes.append("no `source` found; set `payment_method` explicitly or use "
                     "a SetupIntent + confirm flow")
    new_kwargs.append("confirm=True")
    new_kwargs.append('automatic_payment_methods={"enabled": True, "allow_redirects": "never"}')
    return (",\n" + indent).join(new_kwargs), notes


def draft_stripe_charges_fix(repo_path: str | Path, rel_path: str) -> FixDraft:
    """Draft a Charges -> PaymentIntents migration for one Python file."""
    repo = Path(repo_path)
    full = repo / rel_path
    original = full.read_text()
    draft = FixDraft(entry_id="stripe-charges-api")
    new_source = original
    offset_shift = 0
    count = 0

    for m in list(_CHARGE_CALL_RE.finditer(original)):
        start = m.start() + offset_shift
        open_idx = new_source.index("(", start)  # '(' of this call in new_source
        arg_text, after = _extract_balanced_args(new_source, open_idx)
        # preserve the file's indentation style for the rewritten call
        line_start = new_source.rfind("\n", 0, start) + 1
        base_indent = new_source[line_start:start][
            :len(new_source[line_start:start]) - len(new_source[line_start:start].lstrip())]
        arg_indent = base_indent + "    "
        new_args, notes = _migrate_charge_kwargs(arg_text, indent=arg_indent)
        draft.notes.extend(notes)
        replacement = (f"stripe.PaymentIntent.create(\n{arg_indent}"
                       f"{new_args}\n{base_indent})")
        new_source = new_source[:start] + replacement + new_source[after:]
        offset_shift += len(replacement) - (after - start)
        count += 1

    if count == 0:
        draft.notes.append("no stripe.Charge.create calls found — nothing to draft")
        return draft

    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
    draft.changes.append(FileChange(path=rel_path, diff=diff, new_content=new_source))
    draft.notes.insert(0, f"rewrote {count} stripe.Charge.create call(s) to "
                          "stripe.PaymentIntent.create (SCA-ready)")
    draft.notes.append("verify with stripe-mock or Stripe test clocks before merging; "
                       "handle PaymentIntent.next_action for 3D Secure in your frontend")
    return draft


# ---------------------------------------------------------------------------
# Contract test generator (Stripe PaymentIntent)
# ---------------------------------------------------------------------------

def generate_stripe_contract_test(module: str, func_name: str,
                                  amount: int = 2000, currency: str = "usd",
                                  token: str = "pm_card_visa") -> tuple[str, str]:
    """Generate a pytest contract test sketch for the migrated function.

    Returns (path, content). The test mocks stripe.PaymentIntent.create and
    asserts the call contract the migration promises.
    """
    path = "tests/test_stripe_payment_intent_contract.py"
    content = f'''"""Contract test sketch for the Stripe PaymentIntent migration.

Verifies the app calls stripe.PaymentIntent.create with the SCA-ready
parameter contract. Run with pytest. Uses unittest.mock — no network calls.
"""
from unittest.mock import patch

from {module} import {func_name}


@patch("stripe.PaymentIntent.create")
def test_payment_intent_contract(mock_create):
    mock_create.return_value = {{"id": "pi_test_123", "status": "succeeded"}}

    {func_name}({amount}, "{currency}", "{token}")

    assert mock_create.call_count == 1
    _, kwargs = mock_create.call_args

    # --- contract assertions ---
    assert kwargs["amount"] == {amount}
    assert kwargs["currency"] == "{currency}"
    # legacy `source` must be gone; payment_method is the SCA-ready equivalent
    assert "source" not in kwargs, "legacy Charges param leaked into PaymentIntent call"
    assert kwargs["payment_method"] == "{token}"
    assert kwargs["confirm"] is True
    assert kwargs["automatic_payment_methods"]["enabled"] is True
'''
    return path, content


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def draft_fix(entry_id: str, repo_path: str | Path, rel_path: str,
              **kwargs) -> FixDraft:
    """Dispatch to the right vendor fixer. Raises KeyError if unsupported."""
    if entry_id == "stripe-charges-api":
        draft = draft_stripe_charges_fix(repo_path, rel_path)
        if not draft.empty():
            test_path, test_content = generate_stripe_contract_test(
                module=kwargs.get("module", "app"),
                func_name=kwargs.get("func_name", "create_charge"))
            draft.tests.append((test_path, test_content))
        return draft
    raise KeyError(f"no fix drafter for entry '{entry_id}' (v2: add vendor fixer)")


def write_fix(repo_path: str | Path, draft: FixDraft) -> list[str]:
    """Apply a FixDraft to the repo. Returns list of written relative paths."""
    repo = Path(repo_path)
    written = []
    for change in draft.changes:
        full = repo / change.path
        full.write_text(change.new_content)
        written.append(change.path)
    for test_path, content in draft.tests:
        full = repo / test_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        written.append(test_path)
    return written
