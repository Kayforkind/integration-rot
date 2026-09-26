# Integration-rot Autopilot

[![version](https://img.shields.io/badge/version-0.3.0-blue)](https://github.com/Kayforkind/integration-rot)
[![tests](https://img.shields.io/badge/tests-58%20passing-brightgreen)](https://github.com/Kayforkind/integration-rot)
[![python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Agentic AI for engineering teams: finds the third-party API calls rotting in
your codebase, drafts the migration, proves it with a contract test, and opens
the PR.**

Every API you depend on is a liability with a timer on it. Stripe deprecates
an endpoint, Twilio sunsets a product, SendGrid kills v2 — and the blast
radius hides across dozens of repos until something breaks at 2am.
Integration-rot scans your dependencies *and* your source, matches usage
against a curated deprecation knowledge base (17 entries and growing — each
linked to its vendor source, dates only where the vendor published them),
ranks the risk, drafts a working patch with a contract test, and can open the
pull request for you.

---

## Table of contents

- [How it works](#how-it-works)
- [End-to-end worked example](#end-to-end-worked-example)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [CLI reference](#cli-reference)
- [The deprecation database](#the-deprecation-database)
- [Writing a fixer](#writing-a-fixer)
- [Schema-drift detection](#schema-drift-detection)
- [Proposing pull requests](#proposing-pull-requests)
- [CI integration](#ci-integration)
- [Project structure](#project-structure)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## How it works

```
  target repo
      │
      ▼
┌───────────┐   ┌────────────────┐   ┌──────────┐   ┌─────────┐   ┌──────────┐
│   SCAN    │──▶│  DEPRECATIONS  │──▶│ ANALYZER │──▶│  FIXER  │──▶│ PROPOSER │
│ manifests │   │  17 curated    │   │ ranked   │   │ patch   │   │ GitHub   │
│ + API host│   │  entries +     │   │ risk     │   │ draft + │   │ PR with  │
│ heuristic │   │  feed fetchers │   │ report   │   │ contract│   │ evidence │
└───────────┘   └────────────────┘   └──────────┘   │ test    │   └──────────┘
                                                   └────┬────┘
                                                        ▼
                                                  ┌──────────┐
                                                  │  VERIFY  │
                                                  │ executed │
                                                  │ isolated │
                                                  │ sandbox  │
                                                  └──────────┘
                                                        ▲
                                                   ┌─────────┐
                                                   │  DRIFT  │
                                                   │ OpenAPI │
                                                   │ diffing │
                                                   │ + hist. │
                                                   └─────────┘
```

Each stage, with a real example:

**1. SCAN** — inventory dependencies and find hardcoded vendor API calls.

```bash
$ integration-rot scan demo/sample-app
```

```
Dependencies found in demo/sample-app:
  npm      @sendgrid/mail ^6.5.0 [SendGrid]  (package.json)
  npm      stripe ^8.0.0 [Stripe]  (package.json)
  pypi     requests 2.28.0  (requirements.txt)
  pypi     stripe 2.56.0 [Stripe]  (requirements.txt)

Direct vendor API calls:
  [SendGrid] app.py:28: "https://api.sendgrid.com/api/mail.send.json",
```

The scanner parses `package.json` (+ lockfiles), `requirements.txt`, `go.mod`,
`Gemfile`, and `pom.xml`, maps 64 SDK packages to vendors (plus 13 API
hostnames), and heuristically
spots hardcoded vendor hostnames in source.

**2. MATCH** — cross-reference against the deprecation DB (`check`).

```bash
$ integration-rot check demo/sample-app
```

```
Integration-rot report for demo/sample-app
  critical=0 high=0 medium=2 low=0

  [MEDIUM  ] SendGrid: SendGrid v2 API deprecated — migrate to v3 (no sunset published) (no sunset date)
             - dependency `@sendgrid/mail` ^6.5.0 (npm, package.json)
             - app.py:28: `"https://api.sendgrid.com/api/mail.send.json",` (matches sendgrid-v2-api)
  [MEDIUM  ] Stripe: Legacy Charges API superseded by PaymentIntents (SCA-ready) (no sunset date)
             - dependency `stripe` ^8.0.0 (npm, package.json)
             - app.py:16: `charge = stripe.Charge.create(` (matches stripe-charges-api)
```

Risk is ranked by days-to-sunset (`critical` < 0 days, `high` ≤ 90,
`medium` ≤ 180, else `low`), with `breaking` severity bumping one level.
Exit code is 1 when critical/high findings exist — made for CI.

**3. FIX** — draft a real patch plus a contract test.

```bash
$ integration-rot fix demo/sample-app --entry stripe-charges-api \
      --file app.py --module app --func create_charge
```

```diff
--- a/app.py
+++ b/app.py
@@ -13,11 +13,20 @@

 def create_charge(amount_cents, currency, token, description=""):
     """Charge a card using the legacy Charges API (deprecated pattern)."""
-    charge = stripe.Charge.create(
+    # TODO(manual, required): `token` was a legacy Charges-API card token.
+    # It MUST now be a PaymentMethod ID (pm_...): convert it first — e.g. Stripe's
+    # Dashboard data migration tool for saved cards, or the Payment Element /
+    # Checkout for new cards. Stripe documents `payment_method` as a
+    # PaymentMethod, Card, or compatible Source ID — NOT a raw tok_... token.
+    # https://stripe.com/docs/api/payment_intents/create
+    # https://stripe.com/docs/payments/payment-methods/transitioning
+    charge = stripe.PaymentIntent.create(
         amount=amount_cents,
         currency=currency,
-        source=token,
+        payment_method=token,
         description=description,
+        confirm=True,
+        automatic_payment_methods={"enabled": True, "allow_redirects": "never"}
     )
     return charge
```

…plus a generated `tests/test_stripe_payment_intent_contract.py` that mocks
`stripe.PaymentIntent.create` and asserts the SCA-ready parameter contract —
call *shape* only (it can't prove Stripe accepts the value). The draft
explicitly flags converting the legacy `tok_...` token to a PaymentMethod
(`pm_...`) as a required manual step, with links to Stripe's own migration
docs — the tool does not claim a raw token works as `payment_method`.
Eight migrations ship with fixers today: **Stripe** Charges→PaymentIntents,
**Twilio** Authy→Verify v2, **SendGrid** v2→v3, **Plaid**
`/transactions/get`→`/transactions/sync`, **Slack** RTM→Socket Mode,
**GitHub** `?access_token=`→`Authorization` header, **Salesforce** retired
API versions→v59.0, and **Mailchimp** API 2.0→3.0 (migration draft — v3
operations need manual endpoint mapping).

**4. PROPOSE** — open the PR from your already-pushed branch, with evidence
attached. (It doesn't create the branch, commit, or push, and it doesn't run
the contract test — `verify` runs tests in a sandbox; test-gating `propose`
is on the v0.4 roadmap.)

```bash
$ export GITHUB_TOKEN=ghp_...
$ integration-rot propose ./myrepo --entry stripe-charges-api --file app.py \
      --owner myorg --repo-name myrepo --head fix/stripe-payment-intents
Pull request opened: https://github.com/myorg/myrepo/pull/42
```

The PR body carries the risk finding, the unified diff, the contract test,
and reviewer notes — everything a human needs to approve with confidence.

**5. DRIFT** — catch the changes vendors *don't* announce loudly.

```bash
$ integration-rot drift --vendor stripe \
      --spec https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json

Schema drift for Stripe: Stripe: 0 endpoint(s) added, 0 removed, 0 changed

  No drift detected — pinned snapshot matches the fresh spec. ✓
```

Diffs a pinned OpenAPI snapshot against a fresh spec and reports
added/removed/changed endpoints, parameters, and fields.

---

## End-to-end worked example

The repo ships with `demo/sample-app`, an app that *intentionally* uses
outdated integrations: `stripe.Charge.create` (legacy Charges API) and
`api.sendgrid.com/api/mail.send.json` (legacy SendGrid v2 API). Run the whole pipeline:

```bash
# 1. install
python -m pip install -e ".[dev]"

# 2. full pipeline on the sample app
integration-rot demo
# or: ./demo.sh
```

`demo` runs scan → check → fix against the sample app and prints each stage.
To prove the loop closes, apply the generated patch to a copy and run the
generated contract test:

```bash
cp -r demo/sample-app /tmp/proof
integration-rot fix /tmp/proof --entry stripe-charges-api --file app.py \
    --module app --func create_charge --apply
# fix --apply writes the patched app.py AND tests/test_stripe_payment_intent_contract.py

cd /tmp/proof && python -m pytest tests/test_stripe_payment_intent_contract.py -q
# 1 passed — the migration holds its contract

# or do it in one step: apply the fix to an isolated copy and run the
# contract tests there — the target repo is never touched
integration-rot verify /tmp/proof --entry stripe-charges-api --file app.py
# === evidence: stripe-charges-api on app.py ===
# Contract tests run: 1 (PASS)
```

That's the core loop: **detect → draft → prove → propose.**

---

## Installation

Requires Python ≥ 3.10. No runtime dependencies — the standard library only.

```bash
git clone https://github.com/Kayforkind/integration-rot.git
cd integration-rot
python -m pip install -e ".[dev]"   # dev extra = pytest
```

This installs the `integration-rot` command. Verify with:

```bash
integration-rot --version   # integration-rot 0.3.0
```

---

## Quickstart

```bash
# inventory a repo's third-party API surface
integration-rot scan /path/to/repo

# analyze it against the deprecation DB (console report)
integration-rot check /path/to/repo

# machine-readable reports
integration-rot check /path/to/repo --format json > report.json
integration-rot check /path/to/repo --format md > report.md

# draft a fix (dry run prints the diff + contract test)
integration-rot fix /path/to/repo --entry stripe-charges-api --file app.py

# write the patch and test into the repo
integration-rot fix /path/to/repo --entry stripe-charges-api --file app.py --apply

# execute the fix in an isolated sandbox and run the contract tests there
integration-rot verify /path/to/repo --entry stripe-charges-api --file app.py

# check a vendor's API for schema drift
integration-rot drift --vendor stripe --spec <path-or-URL-to-openapi.json>

# record a timestamped snapshot and diff against the previous one
integration-rot snapshot --vendor stripe --spec <path-or-URL-to-openapi.json>

# scan a vendor changelog feed for new deprecation signals
integration-rot fetch --vendor twilio --feed <rss-or-atom-url>

# open a PR with the fix (needs GITHUB_TOKEN)
export GITHUB_TOKEN=ghp_...
integration-rot propose /path/to/repo --entry stripe-charges-api --file app.py \
    --owner myorg --repo-name myrepo --head fix/stripe-charges
```

---

## CLI reference

### `scan` — inventory third-party API dependencies

```
usage: integration-rot scan [-h] repo
```

```bash
$ integration-rot scan demo/sample-app
Dependencies found in demo/sample-app:
  npm      @sendgrid/mail ^6.5.0 [SendGrid]  (package.json)
  npm      stripe ^8.0.0 [Stripe]  (package.json)
  pypi     requests 2.28.0  (requirements.txt)
  pypi     stripe 2.56.0 [Stripe]  (requirements.txt)

Direct vendor API calls:
  [SendGrid] app.py:28: "https://api.sendgrid.com/api/mail.send.json",
```

Parses `package.json` (+ `package-lock.json` / `yarn.lock` version
resolution), `requirements.txt`, `go.mod`, `Gemfile`, `pom.xml`; maps packages
to vendors via `vendor_map.py`; reports hardcoded vendor API hostnames found
in source.

### `check` — analyze a repo against the deprecation DB

```
usage: integration-rot check [-h] [--db DB] [--today TODAY]
                             [--format {console,json,md}] repo
```

```bash
# console report, exit 1 if critical/high findings (CI-friendly)
$ integration-rot check /path/to/repo; echo "exit: $?"
exit: 1

# JSON for automation (real output, evidence trimmed)
$ integration-rot check /path/to/repo --format json
{
  "repo": "<repo>",
  "generated": "<timestamp>",
  "counts": {"critical": 0, "high": 0, "medium": 2, "low": 0},
  "findings": [
    {
      "id": "sendgrid-v2-api",
      "vendor": "SendGrid",
      "title": "SendGrid v2 API deprecated — migrate to v3 (no sunset published)",
      "risk": "medium",
      "days_to_sunset": null,
      "sunset": null,
      ...
    }
  ]
}

# Markdown for PR comments / wikis
$ integration-rot check /path/to/repo --format md

# pin "today" for reproducible reports (used by tests and the demo)
$ integration-rot check /path/to/repo --today 2026-09-26

# use a custom DB (e.g. your org's private fork with internal APIs)
$ integration-rot check /path/to/repo --db ./our-deprecations.json
```

**Flags:**

| Flag | Effect |
|------|--------|
| `--db PATH` | Use a custom `deprecations.json` instead of the bundled DB |
| `--today YYYY-MM-DD` | Override "today" for risk ranking (reproducible reports) |
| `--format {console,json,md}` | Report format (default: `console`) |

### `fix` — draft a fix for a deprecation entry

```
usage: integration-rot fix [-h] --entry ENTRY --file FILE [--module MODULE]
                           [--func FUNC] [--param KEY=VALUE] [--apply] repo
```

```bash
# dry run: print the unified diff + generated contract test + notes
$ integration-rot fix demo/sample-app --entry stripe-charges-api \
      --file app.py --module app --func create_charge

# Twilio fixer needs the local function names (passed as params)
$ integration-rot fix ./myapp --entry twilio-authy-api --file auth.py \
      --module auth \
      --param start_func=start_verification \
      --param check_func=check_verification

# write the patch and the contract test into the repo
$ integration-rot fix ./myapp --entry sendgrid-v2-api --file mailer.py --apply
Applied. Wrote: mailer.py, tests/test_sendgrid_v3_contract.py
```

**Flags:**

| Flag | Effect |
|------|--------|
| `--entry ID` | **(required)** deprecation entry id, e.g. `stripe-charges-api` |
| `--file PATH` | **(required)** repo-relative source file to patch |
| `--module NAME` | Python module name used in the generated test's import (default: `app`) |
| `--func NAME` | Function name used in the generated test (default: `create_charge`) |
| `--param KEY=VALUE` | Extra fixer-specific params, repeatable (e.g. Twilio's `start_func`/`check_func`) |
| `--apply` | Write the patch and contract test to the repo (default is dry-run print) |

Fixers available in v0.3.1:

| Entry id | Migration |
|----------|-----------|
| `stripe-charges-api` | `stripe.Charge.create` → `stripe.PaymentIntent.create` (SCA-ready) |
| `twilio-authy-api` | Authy API → Twilio Verify v2 |
| `sendgrid-v2-api` | `/api/mail.send.json` flat payload → `/v3/mail/send` nested payload |
| `plaid-legacy-transactions` | `/transactions/get` → cursor-based `/transactions/sync` |
| `slack-rtm-api` | RTMClient → Socket Mode |
| `github-api-query-auth` | `?access_token=` in URLs → `Authorization` header (also a credential-leak fix) |
| `salesforce-api-v21-v30` | retired `/services/data/v21.0`–`v30.0` → `v59.0` |
| `mailchimp-api-v2-retirement` | `/2.0/` endpoints → `/3.0/`, `apikey` payload → HTTP basic auth |

### `verify` — executed verification with a migration-evidence bundle

```
usage: integration-rot verify [-h] --entry ENTRY --file FILE [--module MODULE]
                              [--func FUNC] [--param KEY=VALUE] repo
```

`verify` drafts the fix, applies it to an **isolated copy** of the repo, and
runs the generated contract tests with pytest — the migration is not just
drafted, it is *executed*. Your working tree is never touched. Exit code is
0 only if every contract test passes.

```bash
$ integration-rot verify ./myrepo --entry stripe-charges-api --file app.py

=== evidence: stripe-charges-api on app.py ===
Files changed in sandbox: app.py, tests/test_stripe_payment_intent_contract.py
Contract tests run: 1 (PASS)
--- pytest output ---
.                                                                        [100%]
1 passed in 0.16s
--- end pytest output ---
Reviewer notes:
  - rewrote 1 stripe.Charge.create call(s) to stripe.PaymentIntent.create (SCA-ready)
  ...
```

Takes the same `--module` / `--func` / `--param` fixer params as `fix`.
Use it in CI to gate merges on *proven* migrations, not just drafted ones.

### `drift` — detect OpenAPI schema drift

```
usage: integration-rot drift [-h] --vendor VENDOR --spec SPEC
                             [--snapshot SNAPSHOT]
```

```bash
# compare today's Stripe spec against the pinned snapshot
$ integration-rot drift --vendor stripe \
      --spec https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json

Schema drift for Stripe: Stripe: 0 endpoint(s) added, 0 removed, 0 changed

  No drift detected — pinned snapshot matches the fresh spec. ✓

# exit code is 1 when drift IS found — gate deploys on it
$ integration-rot drift --vendor stripe --spec ./candidate-spec.json; echo "exit: $?"
```

**Flags:**

| Flag | Effect |
|------|--------|
| `--vendor NAME` | **(required)** vendor, matches `data/openapi_snapshots/<vendor>.json` |
| `--spec PATH-OR-URL` | **(required)** fresh OpenAPI JSON: local file or `http(s)://` URL |
| `--snapshot PATH` | Override the pinned snapshot path |

### `snapshot` — timestamped OpenAPI snapshots with drift history

While `drift` compares against one pinned snapshot, `snapshot` keeps a
**timestamped history** under `data/openapi_snapshots/<vendor>/` and diffs
each new spec against the previous one — drift over time, not just drift
from the original pin. History directories are gitignored runtime artifacts.

```
usage: integration-rot snapshot [-h] --vendor VENDOR --spec SPEC
```

```bash
$ integration-rot snapshot --vendor stripe --spec ./stripe-spec.json
Saved snapshot: data/openapi_snapshots/stripe/20260926-152345-638706.json
First snapshot — baseline recorded, nothing to diff against yet.

$ integration-rot snapshot --vendor stripe --spec ./stripe-spec-v2.json
Saved snapshot: data/openapi_snapshots/stripe/20260926-152345-806187.json
Diffed against previous snapshot: 20260926-152345-638706.json

Schema drift for stripe: stripe: 1 endpoint(s) added, 0 removed, 0 changed

  [ADDED]   POST /v1/payment_intents/incremental_authorization

# exit code is 1 when drift IS found — run it on a schedule and alert on it
```

### `fetch` — scan a vendor changelog feed for deprecation signals

`fetch` polls a vendor changelog RSS/Atom feed (URL or local XML file),
matches items against deprecation keywords, and prints **candidates for
human review** — a tripwire, not a source of truth. Verify every candidate
against vendor docs before promoting it into `data/deprecations.json`.

```
usage: integration-rot fetch [-h] --vendor VENDOR --feed FEED [--output OUTPUT]
```

```bash
$ integration-rot fetch --vendor twilio --feed https://www.twilio.com/en-us/changelog.rss
1 candidate(s) from https://www.twilio.com/en-us/changelog.rss — REVIEW BEFORE ADDING TO THE DB:

- Authy API deprecation: migrate to Verify v2 (Mon, 01 Feb 2021 00:00:00 GMT)
  signals: deprecat, sunset
  link: https://www.twilio.com/changelog/authy-deprecation

# save the candidates as JSON for a review workflow
$ integration-rot fetch --vendor twilio --feed ./changelog.xml --output /tmp/drafts.json
```

Programmatic use: `RSSChangelogFetcher(vendor, feed_url)` in
`src/integration_rot/deprecations.py` implements the same pipeline and plugs
into the `FETCHERS` registry, so `fetch_all()` can mix live feeds with the
curated DB per vendor.

### `propose` — open a GitHub PR with the fix draft

```
usage: integration-rot propose [-h] --entry ENTRY --file FILE --owner OWNER
                               --repo-name REPO_NAME --head HEAD [--base BASE]
                               [--title TITLE] [--draft] [--module MODULE]
                               [--func FUNC] [--param KEY=VALUE] repo
```

```bash
$ export GITHUB_TOKEN=ghp_...   # needs `repo` scope; never passed as argv
$ integration-rot propose ./myrepo --entry stripe-charges-api --file app.py \
      --owner myorg --repo-name myrepo --head fix/stripe-payment-intents
Pull request opened: https://github.com/myorg/myrepo/pull/42

# open as a draft PR, custom title and base
$ integration-rot propose ./myrepo --entry plaid-legacy-transactions --file sync.py \
      --owner myorg --repo-name myrepo --head fix/plaid-sync --base develop \
      --draft --title "fix: migrate Plaid transactions to /transactions/sync"
```

The PR body includes the risk finding, migration guidance, the full unified
diff, the generated contract test, and reviewer notes. The fix is drafted
from your working tree — commit and push `--head` first, then propose.

**Flags:** `--owner` / `--repo-name` / `--head` required; `--base` (default
`main`), `--title` (default generated), `--draft`, plus the same
`--module` / `--func` / `--param` fixer params as `fix`.

### `demo` — run the full pipeline on the bundled sample app

```bash
$ integration-rot demo     # scan → check → fix, printed in three stages
$ ./demo.sh                # same thing via shell
```

---

## The deprecation database

`data/deprecations.json` is the knowledge base: **17 curated, real**
deprecations across Stripe, Twilio, Slack, SendGrid, Plaid, Twitter, Reddit,
Google, LinkedIn, Mailchimp, Instagram, GitHub, and Salesforce.

### Entry schema (annotated)

```jsonc
{
  // stable id — referenced by `fix --entry` and the fixer registry
  "id": "sendgrid-v2-api",
  // canonical vendor name — must match scanner/vendor_map.py
  "vendor": "SendGrid",
  "title": "SendGrid v2 API deprecated — migrate to v3 (no sunset published)",
  // ISO date (YYYY-MM-DD; YYYY-MM allowed) the deprecation was announced,
  // or "" when the vendor never published an announcement date
  "announced": "",
  // ISO date the API stops working, or null if not announced
  "sunset": null,
  // "breaking" | "warning" | "informational" — feeds risk ranking
  "severity": "warning",
  // SDK deps that imply exposure. Empty = vendor-wide (needs code-pattern evidence).
  "packages": [
    {"ecosystem": "pypi", "name": "sendgrid", "version_spec": ""},
    {"ecosystem": "npm",  "name": "@sendgrid/mail", "version_spec": ""}
  ],
  // regexes matched against repo source. Empty = any usage counts.
  "code_patterns": ["api\\.sendgrid\\.com/api/"],
  // human migration guidance shown in reports and PR bodies
  "migration": "SendGrid encourages v2 API customers to migrate to v3; no sunset date ...",
  // source of truth — every entry links its vendor source
  "source_url": "https://www.twilio.com/docs/sendgrid/for-developers/sending-email/migrating-from-v2-to-v3-mail-send",
  // whether fixer.py can draft a patch for this entry
  "fix_available": true
}
```

### Adding an entry

1. Copy the schema above into `data/deprecations.json`.
2. Keep `announced`/`sunset` as real dates with a `source_url` that backs
   them — entries without a source don't get merged.
3. Run the tests: `python -m pytest -q` (the DB test validates every entry's
   schema, severity, and date parsing).

### Live feed fetchers

`src/integration_rot/deprecations.py` defines a `FeedFetcher` abstraction so
live sources plug in per-vendor without touching the analyzer:

```python
from integration_rot.deprecations import FeedFetcher, FETCHERS
from integration_rot.models import DeprecationEntry

class AcmeChangelogFetcher(FeedFetcher):
    vendor = "Acme"

    def fetch(self) -> list[DeprecationEntry]:
        # poll the changelog, parse deprecation notices, return entries
        ...

FETCHERS["acme"] = AcmeChangelogFetcher()
```

`RSSChangelogFetcher` is a real implementation: it polls an RSS/Atom feed
(stdlib only — no feedparser dependency), matches deprecation keywords, and
returns `informational` entries flagged for review. `GitHubReleasesFetcher`
remains a documented stub — wire in release watching and register the
instance in `FETCHERS`.

---

## Writing a fixer

Fixers live in `src/integration_rot/fixer.py` and follow one pattern:
a `draft_<name>_fix(repo_path, rel_path)` function returning a `FixDraft`
(diff + notes), a `generate_<name>_contract_test(...)` returning
`(path, content)`, and one branch in the `draft_fix` registry.

Annotated example — the Plaid fixer in full:

```python
# 1. compile the patterns that identify the deprecated usage
_PLAID_GET_CALL_RE = re.compile(r"(\w+)\.Transactions\.get\s*\(")
_PLAID_GET_URL_RE = re.compile(r"/transactions/get\b")

def draft_plaid_transactions_fix(repo_path, rel_path) -> FixDraft:
    repo = Path(repo_path)
    original = (repo / rel_path).read_text()
    draft = FixDraft(entry_id="plaid-legacy-transactions")
    new_source = original
    count = 0

    # 2. rewrite each occurrence (iterate reversed so offsets stay valid)
    for m in reversed(list(_PLAID_GET_CALL_RE.finditer(new_source))):
        obj = m.group(1)
        open_idx = new_source.index("(", m.start())
        arg_text, after = _extract_balanced_args(new_source, open_idx)  # shared helper
        parts = _split_top_level_kwargs(arg_text)                        # shared helper
        access_token = parts[0] if parts else "access_token"
        # 3. the actual migration: date-range get -> cursor-based sync
        replacement = f"{obj}.transactions_sync({access_token}, cursor=cursor)"
        new_source = new_source[:m.start()] + replacement + new_source[after:]
        count += 1

    if count == 0:
        draft.notes.append("no /transactions/get usage found — nothing to draft")
        return draft  # empty() -> True; CLI prints notes and exits 0

    # 4. explain what the regex CAN'T do — this honesty is the product
    draft.notes.insert(0, f"rewrote {count} /transactions/get call(s) to /transactions/sync")
    draft.notes.append("response shape differs — /transactions/get returns `transactions`; "
                       "/transactions/sync returns `added` / `modified` / `removed`.")

    # 5. unified diff, like git would show it
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
    draft.changes.append(FileChange(path=rel_path, diff=diff, new_content=new_source))
    return draft

# 6. register it — flip fix_available in data/deprecations.json too
def draft_fix(entry_id, repo_path, rel_path, **kwargs):
    ...
    if entry_id == "plaid-legacy-transactions":
        draft = draft_plaid_transactions_fix(repo_path, rel_path)
        if not draft.empty():
            path, content = generate_plaid_contract_test(
                module=kwargs.get("module", "app"),
                func_name=kwargs.get("func_name", "sync_transactions"))
            draft.tests.append((path, content))
        return draft
```

Shared helpers you should reuse: `_extract_balanced_args` (paren-aware arg
extraction), `_split_top_level_kwargs` (comma-splitting that respects nesting
and strings), and `write_fix` (applies a draft to disk).

---

## Schema-drift detection

Deprecation DBs cover what vendors *announce*. Drift covers what they *do*:
fields appearing, disappearing, or changing type in a live API.

```bash
# pin a snapshot (curated excerpt — see data/openapi_snapshots/stripe.json)
# then diff any fresh spec against it:
integration-rot drift --vendor stripe --spec ./stripe-spec-2026-10.json
```

Example of drift being caught (pinned snapshot vs. a modified spec):

```
Schema drift for Stripe: Stripe: 1 endpoint(s) added, 0 removed, 1 changed

  [ADDED]   POST /v1/terminal/readers
  [CHANGED] POST /v1/charges
            - request field `capture` removed
            - response field `amount` type changed: integer -> string
```

**How it works** (`src/integration_rot/schema_drift.py`):
1. `load_snapshot()` reads the pinned excerpt for the vendor.
2. `load_spec()` fetches the fresh spec (local path or URL) and
   `normalize_spec()` extracts comparable endpoint records — parameters,
   request fields, response fields, with `$ref`s resolved.
3. `diff_specs()` reports added/removed endpoints and, per changed endpoint,
   added/removed/type-changed parameters and fields.

**Adding a vendor snapshot:** extract the endpoints you care about from the
vendor's OpenAPI document into the snapshot shape (`_meta` + `endpoints` with
`path`, `method`, `parameters[]`, `request_fields{}`, `response_fields{}`),
save as `data/openapi_snapshots/<vendor>.json`, and add a self-diff test like
the Stripe one in `tests/test_schema_drift.py`.

---

## Proposing pull requests

`propose` drafts the fix locally, builds a PR body with the risk report, the
diff, the contract test, and reviewer notes, and opens the PR via the GitHub
REST API (`src/integration_rot/proposer.py`, urllib only — no dependencies).

```bash
export GITHUB_TOKEN=ghp_...   # `repo` scope; read from env, never argv
integration-rot propose ./myrepo --entry twilio-authy-api --file auth/otp.py \
    --owner myorg --repo-name myrepo --head fix/twilio-verify \
    --param start_func=start_verification --param check_func=check_verification
```

The generated PR body looks like this (real output, abridged):

```markdown
## 🤖 Integration-rot autopilot

Automated migration draft for **Twilio**: Twilio Authy API end-of-life — migrate to Verify.

**Migration guide:** Migrate one-time passcode flows from the Authy API to the Twilio Verify API (v2). ...
**Vendor source:** https://www.twilio.com/en-us/changelog

### Changes

`authy_app.py`
```diff
-from authy.api import AuthyApiClient
+from twilio.rest import Client  # migrated from authy.api.AuthyApiClient
+VERIFY_SERVICE_SID = "VAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # TODO: your Verify Service SID
 ...
-    resp = authy_api.phones.verification_start(phone_number, country_code, via="sms")
+    resp = authy_api.verify.v2.services(VERIFY_SERVICE_SID).verifications.create(to=f"+{country_code}{phone_number}", channel="sms")
```

### Contract test
`tests/test_twilio_verify_contract.py` ...

### Reviewer notes
- rewrote 4 Authy API call(s) to Twilio Verify v2
- Authy `response.ok()` semantics differ — Verify returns status strings ...
```

Suggested workflow: run `fix --apply` on a branch, push the branch, run the
generated contract test in CI, then `propose` to open the PR.

---

## CI integration

Fail the build when integration rot lands in a PR. Drop this into
`.github/workflows/integration-rot.yml`:

```yaml
name: integration-rot

on:
  pull_request:
  schedule:
    - cron: "0 9 * * 1"   # plus a weekly sweep — vendors don't wait for PRs

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install integration-rot
        run: pip install git+https://github.com/Kayforkind/integration-rot.git
      - name: Check for deprecated API usage
        run: integration-rot check . --format md >> "$GITHUB_STEP_SUMMARY"
      - name: Check for schema drift (Stripe)
        run: >
          integration-rot drift --vendor stripe
          --spec https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json
```

`check` exits 1 on critical/high findings and `drift` exits 1 when drift is
found, so both steps fail the job. The Markdown report lands in the job
summary for reviewers.

---

## Project structure

```
integration-rot/
├── src/integration_rot/
│   ├── __init__.py        version
│   ├── cli.py             scan | check | fix | drift | propose | demo
│   ├── scanner.py         manifest parsing, vendor mapping, API-host heuristic
│   ├── vendor_map.py      64 SDK packages + 13 API hostnames -> canonical vendors
│   ├── deprecations.py    DB loader + FeedFetcher architecture (live feeds plug in here)
│   ├── analyzer.py        matching, risk ranking, console/JSON/Markdown renderers
│   ├── fixer.py           8 migration drafters + contract-test generators + registry
│   ├── schema_drift.py    OpenAPI snapshot diffing (endpoints / params / fields)
│   ├── proposer.py        GitHub PR creation via REST (urllib only)
│   └── models.py          shared dataclasses
├── data/
│   ├── deprecations.json          17 curated deprecation entries
│   └── openapi_snapshots/
│       └── stripe.json            pinned Stripe excerpt (4 endpoints, real fields)
├── demo/
│   └── sample-app/                intentionally-outdated demo target
├── tests/                         pytest suite (83 tests)
├── docs/
│   └── index.html                 project landing page (GitHub Pages)
├── demo.sh                        one-command demo
└── pyproject.toml
```

---

## Roadmap

**v0.3 — live intelligence**
- Wire `RSSChangelogFetcher` / `GitHubReleasesFetcher` for Twilio, Slack, Stripe
- Nightly snapshot refresh + drift history (what changed, when, per vendor)
- Response-schema drift against *observed* traffic, not just specs

**v0.4 — deeper fixes**
- Fixers for the long tail of the DB (Twitter, Reddit, more auth migrations, …)
- Multi-file migrations (a deprecation rarely lives in one file)
- Fix verification: run the contract test *before* proposing the PR

**v0.5 — team workflow**
- GitHub App: install on an org, get PRs on a schedule, no CLI needed
- Monorepo-aware scanning, SARIF output for code-scanning dashboards
- Ignore-files and per-repo policy (`rot.toml`)

---

## Contributing

1. New deprecation entries: add to `data/deprecations.json` following the
   [schema](#entry-schema-annotated) — real dates, real `source_url`, or it
   doesn't merge.
2. New fixers: follow [Writing a fixer](#writing-a-fixer), add tests in
   `tests/test_fixer.py`, flip `fix_available` on the entry.
3. New vendor snapshots: add `data/openapi_snapshots/<vendor>.json` + a
   self-diff test.
4. Run the suite: `python -m pytest -q` — it must be green.

---

## License

MIT © Kayforkind. See [LICENSE](LICENSE).
