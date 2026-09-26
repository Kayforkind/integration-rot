"""Fix drafter.

For deprecation entries flagged `fix_available`, generates a concrete code
patch (unified diff) plus a contract-test sketch. Implemented migrations:
Stripe Charges API -> PaymentIntents, Twilio Authy -> Verify v2,
SendGrid v2 -> v3 mail/send, Plaid /transactions/get -> /transactions/sync,
Slack RTM -> Socket Mode, GitHub ?access_token= -> Authorization header,
Salesforce retired API versions -> v59.0, Mailchimp API 2.0 -> 3.0.
Other entries raise `KeyError` by design.
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
# Twilio: Authy API -> Verify v2
# ---------------------------------------------------------------------------

_AUTHY_IMPORT_RE = re.compile(r"from\s+authy\.api\s+import\s+AuthyApiClient")
_AUTHY_CLIENT_RE = re.compile(r"AuthyApiClient\s*\(")
_VERIFY_START_RE = re.compile(r"(\w+)\.phones\.verification_start\s*\(")
_VERIFY_CHECK_RE = re.compile(r"(\w+)\.phones\.verification_check\s*\(")


def _migrate_verify_start_args(arg_text: str) -> tuple[str, list[str]]:
    """Map Authy verification_start args to Verify v2 verifications.create kwargs."""
    notes = []
    parts = _split_top_level_kwargs(arg_text)
    phone = country = via = None
    positional = []
    for p in parts:
        m = re.match(r"(\w+)\s*=\s*(.*)$", p, re.S)
        if m:
            k, v = m.group(1), m.group(2).strip()
            if k in ("phone_number", "phone"):
                phone = v
            elif k in ("country_code", "country"):
                country = v
            elif k == "via":
                via = v
            else:
                notes.append(f"Authy kwarg `{k}` has no Verify equivalent — dropped, verify manually")
        else:
            positional.append(p)
    if len(positional) > 0:
        phone = phone or positional[0]
    if len(positional) > 1:
        country = country or positional[1]
    if len(positional) > 2:
        via = via or positional[2]
    if phone is None or country is None:
        notes.append("could not map phone/country args — set `to` explicitly")
        to_expr = '"TODO:+E164_PHONE"'
    else:
        to_expr = f'f"+{{{country}}}{{{phone}}}"'
    kwargs = [f"to={to_expr}", f"channel={via or '\"sms\"'}"]
    return ", ".join(kwargs), notes


def _migrate_verify_check_args(arg_text: str) -> tuple[str, list[str]]:
    """Map Authy verification_check args to Verify v2 verification_checks.create."""
    notes = []
    parts = _split_top_level_kwargs(arg_text)
    phone = country = code = None
    positional = []
    for p in parts:
        m = re.match(r"(\w+)\s*=\s*(.*)$", p, re.S)
        if m:
            k, v = m.group(1), m.group(2).strip()
            if k in ("phone_number", "phone"):
                phone = v
            elif k in ("country_code", "country"):
                country = v
            elif k in ("verification_code", "code", "token"):
                code = v
            else:
                notes.append(f"Authy kwarg `{k}` has no Verify equivalent — dropped, verify manually")
        else:
            positional.append(p)
    if len(positional) > 0:
        phone = phone or positional[0]
    if len(positional) > 1:
        country = country or positional[1]
    if len(positional) > 2:
        code = code or positional[2]
    if phone is None or country is None:
        to_expr = '"TODO:+E164_PHONE"'
        notes.append("could not map phone/country args — set `to` explicitly")
    else:
        to_expr = f'f"+{{{country}}}{{{phone}}}"'
    if code is None:
        code = '"TODO:CODE"'
        notes.append("could not map the verification code arg — set `code` explicitly")
    return f"to={to_expr}, code={code}", notes


def draft_twilio_authy_fix(repo_path: str | Path, rel_path: str) -> FixDraft:
    """Draft an Authy API -> Twilio Verify v2 migration for one Python file."""
    repo = Path(repo_path)
    full = repo / rel_path
    original = full.read_text()
    draft = FixDraft(entry_id="twilio-authy-api")
    new_source = original
    count = 0

    if _AUTHY_IMPORT_RE.search(new_source):
        new_source = _AUTHY_IMPORT_RE.sub(
            "from twilio.rest import Client  # migrated from authy.api.AuthyApiClient",
            new_source)
        count += 1
        draft.notes.append("AuthyApiClient(api_key) -> Client(account_sid, auth_token): "
                           "Verify uses your Twilio Account SID + Auth Token, not the Authy key")
    if _AUTHY_CLIENT_RE.search(new_source):
        new_source = _AUTHY_CLIENT_RE.sub("Client(", new_source)
        count += 1

    for rx, migrator, new_call in (
        (_VERIFY_START_RE, _migrate_verify_start_args, "verifications.create"),
        (_VERIFY_CHECK_RE, _migrate_verify_check_args, "verification_checks.create"),
    ):
        for m in reversed(list(rx.finditer(new_source))):
            obj = m.group(1)
            open_idx = new_source.index("(", m.start())
            arg_text, after = _extract_balanced_args(new_source, open_idx)
            new_args, notes = migrator(arg_text)
            draft.notes.extend(notes)
            replacement = (f"{obj}.verify.v2.services(VERIFY_SERVICE_SID)."
                           f"{new_call}({new_args})")
            new_source = new_source[:m.start()] + replacement + new_source[after:]
            count += 1

    if count == 0:
        draft.notes.append("no Authy API usage found — nothing to draft")
        return draft

    if "VERIFY_SERVICE_SID" not in original:
        sid_line = ('VERIFY_SERVICE_SID = "VAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"'
                    "  # TODO: your Verify Service SID\n")
        anchor = "from twilio.rest import Client"
        idx = new_source.find(anchor)
        if idx != -1:
            eol = new_source.index("\n", idx) + 1
            new_source = new_source[:eol] + sid_line + new_source[eol:]
        else:
            new_source = sid_line + "\n" + new_source
    draft.notes.insert(0, f"rewrote {count} Authy API call(s) to Twilio Verify v2")
    draft.notes.append("Authy `response.ok()` semantics differ — Verify returns status "
                       "strings ('pending' / 'approved'); check `.status` explicitly")
    draft.notes.append("create a Verify Service in the Twilio console and store its SID "
                       "in VERIFY_SERVICE_SID")

    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
    draft.changes.append(FileChange(path=rel_path, diff=diff, new_content=new_source))
    return draft


def generate_twilio_contract_test(module: str, start_func: str = "start_verification",
                                  check_func: str = "check_verification") -> tuple[str, str]:
    """Generate a pytest contract test sketch for the Verify v2 migration."""
    path = "tests/test_twilio_verify_contract.py"
    content = f'''"""Contract test sketch for the Twilio Authy -> Verify v2 migration.

Asserts the app drives the Verify v2 API (services -> verifications /
verification_checks) instead of the retired Authy API. Uses unittest.mock.
"""
from unittest.mock import patch

from {module} import {start_func}, {check_func}


def _verify_service(mock_client_cls):
    return mock_client_cls.return_value.verify.v2.services.return_value


@patch("{module}.Client")
def test_verify_start_contract(mock_client_cls):
    svc = _verify_service(mock_client_cls)
    svc.verifications.create.return_value.status = "pending"

    {start_func}("4155552671", "1")

    svc.verifications.create.assert_called_once()
    _, kwargs = svc.verifications.create.call_args
    assert kwargs["to"] == "+14155552671"
    assert kwargs["channel"] in ("sms", "call", "email")


@patch("{module}.Client")
def test_verify_check_contract(mock_client_cls):
    svc = _verify_service(mock_client_cls)
    svc.verification_checks.create.return_value.status = "approved"

    assert {check_func}("4155552671", "1", "123456") is not None

    svc.verification_checks.create.assert_called_once()
    _, kwargs = svc.verification_checks.create.call_args
    assert kwargs["to"] == "+14155552671"
    assert kwargs["code"] == "123456"
'''
    return path, content


# ---------------------------------------------------------------------------
# SendGrid: v2 mail/send -> v3 mail/send
# ---------------------------------------------------------------------------

_SG_V2_URL_RE = re.compile(r"api\.sendgrid\.com/v2/mail/send")
_SG_CLIENT_RE = re.compile(r"sendgrid\.SendGridClient\s*\(")
_SG_PAYLOAD_RE = re.compile(r"(data|json)\s*=\s*\{")


def _migrate_sendgrid_payload(source: str, start: int) -> tuple[str, str, list[str]]:
    """Rewrite a v2 flat mail payload dict to the v3 nested structure.

    Returns (new_source, new_end_note, notes). If the payload can't be mapped
    safely, returns the source unchanged with notes explaining the manual step.
    """
    notes = []
    brace_idx = source.index("{", start)
    # balanced-brace extraction
    depth, i = 0, brace_idx
    in_str: str | None = None
    escaped = False
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
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        i += 1
    dict_text = source[brace_idx:i + 1]
    inner = dict_text[1:-1]
    kv_re = re.compile(r"""(['"])((?:\\.|(?!\1).)*)\1\s*:\s*""")
    pairs: list[tuple[str, str]] = []
    pos = 0
    for part in _split_top_level_kwargs(inner):
        m = kv_re.match(part.strip())
        if not m:
            notes.append(f"could not parse payload entry `{part.strip()[:50]}` — "
                         "migrate the v2 payload to v3 personalizations manually")
            return source, "", notes
        pairs.append((m.group(2), part.strip()[m.end():].strip()))
    vals = dict(pairs)
    to_v, from_v = vals.get("to"), vals.get("from")
    subject_v, text_v, html_v = vals.get("subject"), vals.get("text"), vals.get("html")
    unmapped = [k for k in vals if k not in ("to", "from", "subject", "text", "html")]
    if unmapped:
        notes.append(f"v2 payload keys have no direct v3 mapping, carried as "
                     f"x-smtpapi note — verify: {', '.join(unmapped)}")
    if to_v is None:
        notes.append("no `to` in v2 payload — set personalizations[].to explicitly")
        return source, "", notes
    content_items = []
    if text_v is not None:
        content_items.append(f'{{"type": "text/plain", "value": {text_v}}}')
    if html_v is not None:
        content_items.append(f'{{"type": "text/html", "value": {html_v}}}')
    if not content_items:
        notes.append("no `text`/`html` body in v2 payload — add v3 `content` explicitly")
        return source, "", notes
    lines = ['{"personalizations": [{"to": [{"email": %s}]}]' % to_v]
    if from_v is not None:
        lines.append(f'"from": {{"email": {from_v}}}')
    if subject_v is not None:
        lines.append(f'"subject": {subject_v}')
    lines.append(f'"content": [{", ".join(content_items)}]')
    # rebuild the dict with clean indentation, and switch data= -> json=
    # (v3 requires a JSON body, not form-encoded data)
    line_start = source.rfind("\n", 0, start) + 1
    base_indent = re.match(r"\s*", source[line_start:]).group(0)
    inner = base_indent + "    "
    new_dict = "{\n" + ",\n".join(inner + ln for ln in lines) + ",\n" + base_indent + "}"
    new_source = source[:start] + "json=" + new_dict + source[i + 1:]
    notes.insert(0, "rewrote the v2 flat mail payload to the v3 nested structure "
                    "(personalizations / from / content)")
    return new_source, "", notes


def draft_sendgrid_v2_fix(repo_path: str | Path, rel_path: str) -> FixDraft:
    """Draft a SendGrid v2 -> v3 mail/send migration for one Python file."""
    repo = Path(repo_path)
    full = repo / rel_path
    original = full.read_text()
    draft = FixDraft(entry_id="sendgrid-v2-api")
    new_source = original
    count = 0

    if _SG_V2_URL_RE.search(new_source):
        new_source = _SG_V2_URL_RE.sub("api.sendgrid.com/v3/mail/send", new_source)
        count += 1
        draft.notes.append("endpoint moved to /v3/mail/send — v3 requires the nested "
                           "JSON payload and a v3 API key (same key usually works)")
    if _SG_CLIENT_RE.search(new_source):
        new_source = _SG_CLIENT_RE.sub("sendgrid.SendGridAPIClient(", new_source)
        count += 1
        draft.notes.append("SendGridClient -> SendGridAPIClient (v3 SDK class)")

    for m in reversed(list(_SG_PAYLOAD_RE.finditer(new_source))):
        if "sendgrid.com/v3" not in new_source[max(0, m.start() - 200):m.start() + 400]:
            continue  # only rewrite payloads near a v3 URL
        new_source, _, notes = _migrate_sendgrid_payload(new_source, m.start())
        draft.notes.extend(notes)
        count += 1

    if count == 0:
        draft.notes.append("no SendGrid v2 usage found — nothing to draft")
        return draft
    draft.notes.insert(0, f"rewrote {count} SendGrid v2 usage(s) to v3")
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
    draft.changes.append(FileChange(path=rel_path, diff=diff, new_content=new_source))
    return draft


def generate_sendgrid_contract_test(module: str, func_name: str = "send_email") -> tuple[str, str]:
    """Generate a pytest contract test sketch for the SendGrid v3 migration."""
    path = "tests/test_sendgrid_v3_contract.py"
    content = f'''"""Contract test sketch for the SendGrid v2 -> v3 migration.

Asserts the app POSTs to /v3/mail/send with the v3 nested payload shape.
Uses unittest.mock — no network calls.
"""
from unittest.mock import patch

import requests

from {module} import {func_name}


@patch("requests.post")
def test_sendgrid_v3_contract(mock_post):
    mock_post.return_value.status_code = 202

    {func_name}("user@example.com", "Hello", "plain body")

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    url = args[0] if args else kwargs.get("url", "")
    assert url.rstrip("/").endswith("/v3/mail/send"), f"must hit v3 endpoint, got {{url}}"

    payload = kwargs.get("json") or kwargs.get("data") or {{}}
    assert "personalizations" in payload, "v3 payload needs personalizations"
    assert payload["personalizations"][0]["to"][0]["email"] == "user@example.com"
    assert payload["subject"] == "Hello"
    assert any(c["type"] == "text/plain" for c in payload["content"])
'''
    return path, content


# ---------------------------------------------------------------------------
# Plaid: /transactions/get -> /transactions/sync
# ---------------------------------------------------------------------------

_PLAID_GET_CALL_RE = re.compile(r"(\w+)\.Transactions\.get\s*\(")
_PLAID_GET_URL_RE = re.compile(r"/transactions/get\b")


def draft_plaid_transactions_fix(repo_path: str | Path, rel_path: str) -> FixDraft:
    """Draft a Plaid /transactions/get -> /transactions/sync migration."""
    repo = Path(repo_path)
    full = repo / rel_path
    original = full.read_text()
    draft = FixDraft(entry_id="plaid-legacy-transactions")
    new_source = original
    count = 0

    for m in reversed(list(_PLAID_GET_CALL_RE.finditer(new_source))):
        obj = m.group(1)
        open_idx = new_source.index("(", m.start())
        arg_text, after = _extract_balanced_args(new_source, open_idx)
        parts = _split_top_level_kwargs(arg_text)
        access_token = parts[0] if parts else "access_token"
        replacement = f"{obj}.transactions_sync({access_token}, cursor=cursor)"
        new_source = new_source[:m.start()] + replacement + new_source[after:]
        count += 1
    if _PLAID_GET_URL_RE.search(new_source):
        new_source = _PLAID_GET_URL_RE.sub("/transactions/sync", new_source)
        count += 1

    if count == 0:
        draft.notes.append("no /transactions/get usage found — nothing to draft")
        return draft

    draft.notes.insert(0, f"rewrote {count} /transactions/get call(s) to /transactions/sync")
    draft.notes.append("/transactions/sync is cursor-based, not date-range: initialize "
                       "`cursor = None`, loop while `response['has_more']`, and persist "
                       "`response['next_cursor']` between runs for incremental sync")
    draft.notes.append("response shape differs — /transactions/get returns `transactions`; "
                       "/transactions/sync returns `added` / `modified` / `removed`. "
                       "Update downstream code accordingly")
    draft.notes.append("date-range args (start_date/end_date) were dropped — sync covers "
                       "the full available history on first run")
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
    draft.changes.append(FileChange(path=rel_path, diff=diff, new_content=new_source))
    return draft


def generate_plaid_contract_test(module: str, func_name: str = "sync_transactions") -> tuple[str, str]:
    """Generate a pytest contract test sketch for the /transactions/sync migration."""
    path = "tests/test_plaid_sync_contract.py"
    content = f'''"""Contract test sketch for the Plaid /transactions/get -> /transactions/sync migration.

Asserts the app drives the cursor-based sync endpoint and pages through
`has_more`. Uses unittest.mock — no network calls.
"""
from unittest.mock import MagicMock, patch

from {module} import {func_name}


@patch("{module}.plaid_client")
def test_plaid_sync_contract(mock_client):
    mock_client.transactions_sync.side_effect = [
        {{"added": [{{"transaction_id": "t1"}}], "has_more": True, "next_cursor": "c1"}},
        {{"added": [], "has_more": False, "next_cursor": "c2"}},
    ]

    added = {func_name}("access-test-token")

    assert mock_client.transactions_sync.called
    # first call starts a fresh sync when no cursor is stored
    _, kwargs = mock_client.transactions_sync.call_args_list[0]
    assert kwargs.get("cursor") is None
    assert added == [{{"transaction_id": "t1"}}]
'''
    return path, content


# ---------------------------------------------------------------------------
# Slack: RTM API -> Socket Mode
# ---------------------------------------------------------------------------

_RTM_IMPORT_RE = re.compile(r"from\s+slack\s+import\s+RTMClient")
_RTM_CLIENT_RE = re.compile(r"(\w+)\s*=\s*RTMClient\(token\s*=\s*([^)]+)\)")
_RTM_RUN_ON_RE = re.compile(r"@RTMClient\.run_on\(event\s*=\s*['\"]([^'\"]+)['\"]\)")


def draft_slack_rtm_fix(repo_path: str | Path, rel_path: str) -> FixDraft:
    """Draft a Slack RTM -> Socket Mode migration for one Python file."""
    repo = Path(repo_path)
    full = repo / rel_path
    original = full.read_text()
    draft = FixDraft(entry_id="slack-rtm-api")
    new_source = original
    count = 0
    old_var: str | None = None

    if _RTM_IMPORT_RE.search(new_source):
        new_source = _RTM_IMPORT_RE.sub(
            "from slack_sdk import WebClient\n"
            "from slack_sdk.socket_mode import SocketModeClient",
            new_source)
        count += 1
    m = _RTM_CLIENT_RE.search(new_source)
    if m:
        old_var = m.group(1)
        token = m.group(2).strip()
        replacement = (
            'socket_client = SocketModeClient(\n'
            '    app_token="xapp-REPLACE-ME",  # TODO: Socket Mode needs an app-level token (xapp-...)\n'
            f'    web_client=WebClient(token={token}),\n'
            ')')
        new_source = new_source[:m.start()] + replacement + new_source[m.end():]
        count += 1
        draft.notes.append("RTMClient(token=...) -> SocketModeClient(app_token=..., "
                           "web_client=WebClient(token=...)): Socket Mode requires an "
                           "app-level token (starts with xapp-), not a bot token")
    for m in reversed(list(_RTM_RUN_ON_RE.finditer(new_source))):
        event = m.group(1)
        replacement = (
            f"# TODO(socket-mode): RTM event '{event}' — register the handler below via\n"
            f"# socket_client.socket_mode_request_listeners.append(<handler_function>)")
        new_source = new_source[:m.start()] + replacement + new_source[m.end():]
        count += 1
        draft.notes.append(
            f"@RTMClient.run_on(event='{event}') has no decorator equivalent — "
            "append the handler to socket_client.socket_mode_request_listeners; "
            "the handler signature becomes (client: SocketModeClient, req: SocketModeRequest)")
    if old_var:
        start_re = re.compile(rf"\b{re.escape(old_var)}\.start\(\)")
        if start_re.search(new_source):
            new_source = start_re.sub("socket_client.connect()", new_source)
            count += 1

    if count == 0:
        draft.notes.append("no Slack RTM usage found — nothing to draft")
        return draft
    draft.notes.insert(0, f"rewrote {count} Slack RTM usage(s) to Socket Mode")
    draft.notes.append("enable Socket Mode in your Slack app settings and generate an "
                       "app-level token with the connections:write scope")
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
    draft.changes.append(FileChange(path=rel_path, diff=diff, new_content=new_source))
    return draft


def generate_slack_contract_test(module: str = "app") -> tuple[str, str]:
    """Generate a pytest contract test sketch for the Socket Mode migration."""
    path = "tests/test_slack_socket_mode_contract.py"
    content = f'''"""Contract test sketch for the Slack RTM -> Socket Mode migration.

Asserts the app builds a SocketModeClient with an app-level token and a
WebClient, registers a listener, and connects. Uses unittest.mock.
"""
from unittest.mock import patch


@patch("{module}.SocketModeClient")
@patch("{module}.WebClient")
def test_socket_mode_contract(mock_web_client, mock_socket_client):
    import {module} as app
    import importlib
    importlib.reload(app)

    mock_socket_client.assert_called_once()
    _, kwargs = mock_socket_client.call_args
    assert kwargs["app_token"].startswith("xapp-"), "Socket Mode needs an app-level token"
    assert "web_client" in kwargs
    mock_socket_client.return_value.connect.assert_called()
    assert mock_socket_client.return_value.socket_mode_request_listeners, \\
        "at least one listener must be registered"
'''
    return path, content


# ---------------------------------------------------------------------------
# GitHub: ?access_token= query param -> Authorization header
# ---------------------------------------------------------------------------

_GH_URL_LIT_RE = re.compile(
    r"(?P<p>[fF]?)(?P<q>['\"])(?P<url>[^'\"]*?access_token=[^'\"]*?)(?P=q)")
_REQ_CALL_RE = re.compile(
    r"requests\.(get|post|put|patch|delete|head|options|request)\s*\(")


def _enclosing_requests_call(source: str, pos: int):
    """Return (match, open_idx, after) for the innermost requests.*() call
    containing `pos`, or None."""
    best = None
    for m in _REQ_CALL_RE.finditer(source):
        open_idx = m.end() - 1
        try:
            _, after = _extract_balanced_args(source, open_idx)
        except ValueError:
            continue
        if m.start() <= pos < after:
            best = (m, open_idx, after)  # later matches are more deeply nested
    return best


def _strip_token_param(url: str) -> str:
    """Remove the access_token query parameter, keeping the rest of the URL."""
    new_url = re.sub(r"\?access_token=[^&]*&", "?", url)
    return re.sub(r"[?&]access_token=[^&]*", "", new_url)


def _token_expr(url: str, is_fstring: bool, after_literal: str
               ) -> tuple[str, bool, list[str], int]:
    """Extract the token expression from a URL containing access_token=.

    Returns (expr, is_literal_secret, notes, concat_len) where concat_len is
    the number of source characters after the string literal that form the
    `+ token` concatenation (0 when the token is inside the literal).
    """
    notes: list[str] = []
    tm = re.search(r"[?&]access_token=([^&]*)", url)
    part = tm.group(1) if tm else ""
    if is_fstring and part.startswith("{"):
        inner = part[1:].rstrip("}")
        expr = inner.split("!")[0].split(":")[0].strip() or "TODO_TOKEN"
        return expr, False, notes, 0
    if part == "" and not is_fstring:
        cm = re.match(r"(\s*\+\s*)([A-Za-z_][A-Za-z0-9_\.]*)", after_literal)
        if cm:
            return cm.group(2), False, notes, len(cm.group(0))
    if part and not part.startswith("{"):
        notes.append("the access token was hardcoded in source — move it to an "
                     "environment variable or secret manager")
        return repr(part), True, notes, 0
    notes.append("could not resolve the token expression — set the Authorization "
                 "header value explicitly (marked TODO)")
    return '"TODO_TOKEN"', True, notes, 0


def draft_github_query_auth_fix(repo_path: str | Path, rel_path: str) -> FixDraft:
    """Draft a GitHub ?access_token= -> Authorization header migration."""
    repo = Path(repo_path)
    full = repo / rel_path
    original = full.read_text()
    draft = FixDraft(entry_id="github-api-query-auth")
    new_source = original
    count = 0

    for m in reversed(list(_GH_URL_LIT_RE.finditer(original))):
        lit_start, lit_end = m.span()
        is_f = bool(m.group("p"))
        quote = m.group("q")
        url = m.group("url")
        # after_literal comes from the pristine source: later (already-applied)
        # replacements sit after this match and must not pollute it.
        expr, is_literal, notes, concat_len = _token_expr(
            url, is_f, original[lit_end:lit_end + 60])
        draft.notes.extend(notes)
        new_url = _strip_token_param(url)
        drop_literal = False
        if new_url == "":
            # the literal was only the token fragment (string concatenation):
            # drop it together with the preceding `+`
            pre = new_source[:lit_start]
            m2 = re.search(r"\+\s*$", pre)
            if m2:
                lit_start = m2.start()
                drop_literal = True
        replacement = "" if drop_literal else (("f" if is_f else "")
                      + quote + new_url + quote)
        new_source = (new_source[:lit_start] + replacement
                      + new_source[lit_end + concat_len:])

        call = _enclosing_requests_call(new_source, lit_start)
        if call is None:
            draft.notes.append(
                "URL is not inside a requests.*() call — add "
                f"headers={{\"Authorization\": \"Bearer \" + {expr}}} manually")
            count += 1
            continue
        _, open_idx, after = call
        arg_text, _ = _extract_balanced_args(new_source, open_idx)
        if re.search(r"(^|,)\s*headers\s*=", arg_text):
            draft.notes.append(
                "call already passes headers= — merge "
                f"'Authorization': 'Bearer {{...}}' ({expr}) into it manually")
        else:
            if is_f or (expr.isidentifier()):
                header_code = 'f"Bearer {' + expr + '}"'
            else:
                header_code = f'"Bearer " + {expr}'
            insertion = f', headers={{"Authorization": {header_code}}}'
            new_source = new_source[:after - 1] + insertion + new_source[after - 1:]
        count += 1

    if re.search(r"[?&]client_(id|secret)=", new_source):
        draft.notes.append(
            "client_id/client_secret in the query string also needs migration — "
            "use HTTP Basic auth (base64 client_id:client_secret) on the OAuth "
            "token endpoint instead; left for manual review")

    if count == 0:
        draft.notes.append("no ?access_token= usage found — nothing to draft")
        return draft
    draft.notes.insert(0, f"moved {count} access_token(s) from URL query string "
                          "to the Authorization header")
    draft.notes.append("GitHub removed query-param auth in Sep 2021 — tokens in URLs "
                       "also leak into logs and caches, so this is a security fix too")
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
    draft.changes.append(FileChange(path=rel_path, diff=diff, new_content=new_source))
    return draft


def generate_github_contract_test(rel_path: str) -> tuple[str, str]:
    """Source-level contract test: no credentials in URLs, header auth used."""
    path = "tests/test_github_auth_header_contract.py"
    content = f'''"""Contract test for the GitHub query-param auth -> Authorization header migration.

Source-level contract: credentials must not travel in URL query strings and
requests must carry an Authorization header. Runs with pytest, no network.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "{rel_path}"


def test_no_query_param_credentials():
    src = SRC.read_text()
    assert "access_token=" not in src, \\
        "credential still passed via URL query string"


def test_authorization_header_present():
    src = SRC.read_text()
    assert "Authorization" in src, \\
        "migrated code must send an Authorization header"
'''
    return path, content


# ---------------------------------------------------------------------------
# Salesforce: Platform API v21.0-v30.0 -> v59.0
# ---------------------------------------------------------------------------

_SF_URL_RE = re.compile(r"/services/data/v(2[1-9]|30)(\.\d+)?")
_SF_VERSION_KWARG_RE = re.compile(r"\bversion\s*=\s*(['\"])(2[1-9]|30)(\.\d+)?\1")
_SF_TARGET = "v59.0"


def draft_salesforce_version_fix(repo_path: str | Path, rel_path: str) -> FixDraft:
    """Draft a Salesforce retired API version -> v59.0 migration."""
    repo = Path(repo_path)
    full = repo / rel_path
    original = full.read_text()
    draft = FixDraft(entry_id="salesforce-api-v21-v30")
    new_source = original
    count = 0

    new_source, n = _SF_URL_RE.subn("/services/data/" + _SF_TARGET, new_source)
    count += n

    def _kwarg_sub(m):
        return f"version={m.group(1)}{_SF_TARGET.lstrip('v')}{m.group(1)}"
    new_source, n = _SF_VERSION_KWARG_RE.subn(_kwarg_sub, new_source)
    count += n

    if count == 0:
        draft.notes.append("no retired Salesforce API version usage found — nothing to draft")
        return draft
    draft.notes.insert(0, f"bumped {count} Salesforce API version reference(s) to {_SF_TARGET}")
    draft.notes.append("v21.0–v30.0 are retired and return errors; v59.0 (Winter '24) is a "
                       "safe, long-supported target — newer versions exist if you want them")
    draft.notes.append("verify response shapes: some objects gained/renamed fields between "
                       "v30 and v59 — run your integration tests against a sandbox")
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
    draft.changes.append(FileChange(path=rel_path, diff=diff, new_content=new_source))
    return draft


def generate_salesforce_contract_test(rel_path: str) -> tuple[str, str]:
    """Source-level contract test: retired versions gone, v59.0 used."""
    path = "tests/test_salesforce_version_contract.py"
    content = f'''"""Contract test for the Salesforce retired-version -> v59.0 migration.

Source-level contract: no references to retired v21.0–v30.0 remain, and the
supported target version is used. Runs with pytest, no network.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "{rel_path}"
RETIRED_RE = re.compile(r"/services/data/v(2[1-9]|30)\\.")


def test_no_retired_versions():
    src = SRC.read_text()
    assert not RETIRED_RE.search(src), "retired Salesforce API version still referenced"


def test_supported_version_used():
    src = SRC.read_text()
    assert "/services/data/v59.0" in src or "'59.0'" in src or '"59.0"' in src, \\
        "expected the v59.0 target version in migrated code"
'''
    return path, content


# ---------------------------------------------------------------------------
# Mailchimp: API 2.0 -> Marketing API 3.0
# ---------------------------------------------------------------------------

_MC_URL_RE = re.compile(r"https://([A-Za-z0-9-]+)\.api\.mailchimp\.com/2\.0/")
_MC_APIKEY_RE = re.compile(r"""(['"])apikey\1\s*:\s*([^,}\n]+),?""")
_MC_SDK_RE = re.compile(r"mailchimp\.Mailchimp\s*\(")


def draft_mailchimp_v2_fix(repo_path: str | Path, rel_path: str) -> FixDraft:
    """Draft a Mailchimp API 2.0 -> 3.0 migration for one Python file."""
    repo = Path(repo_path)
    full = repo / rel_path
    original = full.read_text()
    draft = FixDraft(entry_id="mailchimp-api-v2-retirement")
    new_source = original
    count = 0

    new_source, n = _MC_URL_RE.subn(r"https://\1.api.mailchimp.com/3.0/", new_source)
    count += n
    if n:
        draft.notes.append("endpoint base moved to /3.0/ — v3 endpoints are "
                           "resource-oriented and differ per operation (see notes)")

    # apikey in payload/query -> HTTP basic auth. Scope to mailchimp calls.
    for m in reversed(list(_REQ_CALL_RE.finditer(new_source))):
        open_idx = m.end() - 1
        try:
            arg_text, after = _extract_balanced_args(new_source, open_idx)
        except ValueError:
            continue
        if "api.mailchimp.com" not in arg_text:
            continue
        km = _MC_APIKEY_RE.search(arg_text)
        if not km:
            continue
        expr = km.group(2).strip()
        arg_text = _MC_APIKEY_RE.sub("", arg_text, count=1)
        if not re.search(r"(^|,)\s*auth\s*=", arg_text):
            arg_text = arg_text.rstrip()
            if arg_text.endswith(","):
                arg_text = arg_text[:-1].rstrip()
            arg_text = arg_text + f", auth=('', {expr})"
        else:
            draft.notes.append("call already passes auth= — dropped the apikey payload "
                               "entry; confirm the existing auth uses the API key")
        new_source = new_source[:open_idx + 1] + arg_text + new_source[after - 1:]
        count += 1
        draft.notes.append(f"moved apikey from the payload to HTTP basic auth "
                           f"(auth=('', {expr})) — v3 authenticates as user 'anystring' "
                           "with the API key as password")

    if _MC_SDK_RE.search(new_source):
        draft.notes.append("mailchimp.Mailchimp() (v2 SDK) has no drop-in v3 equivalent — "
                           "switch to the mailchimp-marketing package and configure with "
                           "client.set_config({'api_key': KEY, 'server': 'usX'})")

    if count == 0:
        draft.notes.append("no Mailchimp API 2.0 usage found — nothing to draft")
        return draft
    draft.notes.insert(0, f"rewrote {count} Mailchimp API 2.0 usage(s) toward 3.0")
    draft.notes.append("v3 payloads are operation-specific and NOT a rename of v2: e.g. "
                       "/2.0/lists/subscribe -> POST /3.0/lists/{list_id}/members with "
                       "{'email_address': ..., 'status': 'subscribed'} — remap each call "
                       "against the v3 reference before merging")
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
    draft.changes.append(FileChange(path=rel_path, diff=diff, new_content=new_source))
    return draft


def generate_mailchimp_contract_test(rel_path: str) -> tuple[str, str]:
    """Source-level contract test: v2 endpoints/auth gone, v3 in use."""
    path = "tests/test_mailchimp_v3_contract.py"
    content = f'''"""Contract test for the Mailchimp API 2.0 -> 3.0 migration.

Source-level contract: no v2 endpoints or apikey-in-payload auth remain.
Runs with pytest, no network.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "{rel_path}"


def test_no_v2_endpoints():
    src = SRC.read_text()
    assert "api.mailchimp.com/2.0" not in src, "Mailchimp API v2 endpoint still referenced"


def test_no_apikey_in_payload():
    src = SRC.read_text()
    assert '"apikey"' not in src and "'apikey'" not in src, \\
        "v2 apikey auth still in payload — v3 uses HTTP basic auth"


def test_v3_base_used():
    src = SRC.read_text()
    assert "api.mailchimp.com/3.0" in src, "expected the v3 API base in migrated code"
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
    if entry_id == "twilio-authy-api":
        draft = draft_twilio_authy_fix(repo_path, rel_path)
        if not draft.empty():
            test_path, test_content = generate_twilio_contract_test(
                module=kwargs.get("module", "app"),
                start_func=kwargs.get("start_func", "start_verification"),
                check_func=kwargs.get("check_func", "check_verification"))
            draft.tests.append((test_path, test_content))
        return draft
    if entry_id == "sendgrid-v2-api":
        draft = draft_sendgrid_v2_fix(repo_path, rel_path)
        if not draft.empty():
            test_path, test_content = generate_sendgrid_contract_test(
                module=kwargs.get("module", "app"),
                func_name=kwargs.get("func_name", "send_email"))
            draft.tests.append((test_path, test_content))
        return draft
    if entry_id == "plaid-legacy-transactions":
        draft = draft_plaid_transactions_fix(repo_path, rel_path)
        if not draft.empty():
            test_path, test_content = generate_plaid_contract_test(
                module=kwargs.get("module", "app"),
                func_name=kwargs.get("func_name", "sync_transactions"))
            draft.tests.append((test_path, test_content))
        return draft
    if entry_id == "slack-rtm-api":
        draft = draft_slack_rtm_fix(repo_path, rel_path)
        if not draft.empty():
            test_path, test_content = generate_slack_contract_test(
                module=kwargs.get("module", "app"))
            draft.tests.append((test_path, test_content))
        return draft
    if entry_id == "github-api-query-auth":
        draft = draft_github_query_auth_fix(repo_path, rel_path)
        if not draft.empty():
            test_path, test_content = generate_github_contract_test(rel_path)
            draft.tests.append((test_path, test_content))
        return draft
    if entry_id == "salesforce-api-v21-v30":
        draft = draft_salesforce_version_fix(repo_path, rel_path)
        if not draft.empty():
            test_path, test_content = generate_salesforce_contract_test(rel_path)
            draft.tests.append((test_path, test_content))
        return draft
    if entry_id == "mailchimp-api-2-retirement":
        draft = draft_mailchimp_v2_fix(repo_path, rel_path)
        if not draft.empty():
            test_path, test_content = generate_mailchimp_contract_test(rel_path)
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
