# Integration-rot Autopilot — MVP

Agentic AI for engineering teams: monitors third-party API dependencies,
detects deprecations and schema drift, and drafts code fixes with contract
tests. This is the **working MVP prototype** (v0.1.0).

## What it does

```
  target repo
      │
      ▼
┌───────────┐   ┌────────────────┐   ┌──────────┐   ┌─────────┐
│  SCANNER  │──▶│  DEPRECATIONS  │──▶│ ANALYZER │──▶│  FIXER  │
│ manifests │   │  curated DB +  │   │ ranked   │   │ patch   │
│ + API host│   │  fetcher stubs │   │ risk     │   │ draft + │
│ heuristic │   │  (live in v2)  │   │ report   │   │ contract│
└───────────┘   └────────────────┘   │          │   │ test    │
                                    ▼          │   ▼         │
                              console/json/md  │  unified diff
                                               │  + pytest sketch
```

1. **Scanner** (`src/integration_rot/scanner.py`) — parses `package.json`
   (+ lockfile version resolution), `requirements.txt`, `go.mod`, `Gemfile`,
   `pom.xml`; maps packages to vendors (`vendor_map.py`); heuristically finds
   hardcoded vendor API URLs in source.
2. **Deprecations** (`deprecations.py` + `data/deprecations.json`) — curated
   DB of 7 real deprecations (Stripe, Twilio, Slack, SendGrid, Plaid) with
   sunset dates, migration guidance, source URLs. `FeedFetcher` abstraction
   means live changelog feeds plug in per-vendor later (stubs included).
3. **Analyzer** (`analyzer.py`) — cross-references deps + code patterns
   against the DB, ranks risk (critical/high/medium/low by days-to-sunset),
   renders console / JSON / Markdown reports. Exit code 1 on critical/high
   (CI-friendly).
4. **Fixer** (`fixer.py`) — drafts real patches. MVP implements
   **Stripe `Charge.create` → `PaymentIntent.create`** (SCA-ready kwarg
   mapping, indentation-preserving) plus a generated **contract test sketch**.
   Other vendors raise `KeyError` with a v2 pointer by design.

## Quickstart

```bash
python -m pip install -e ".[dev]"

# full pipeline on the bundled sample app (intentionally outdated Stripe + SendGrid usage)
python -m integration_rot.cli demo
# or: ./demo.sh

# your own repo
integration-rot scan  /path/to/repo
integration-rot check /path/to/repo --format md
integration-rot fix   /path/to/repo --entry stripe-charges-api --file app.py --apply
```

`check` exits 1 when critical/high findings exist — wire it into CI to catch
integration rot on every PR.

## Demo walkthrough

`demo/sample-app` pins `stripe==2.56.0` / `stripe@^8.0.0`, calls
`stripe.Charge.create(...)` with a 2019 API version, and hits
`api.sendgrid.com/v2/mail/send`. Running `demo`:

- **SCAN** finds 4 deps (2 mapped to vendors) + 1 direct vendor API call
- **CHECK** reports 1 CRITICAL (SendGrid v2, sunset 2021-06-30) and
  1 MEDIUM (Stripe Charges API, no sunset date)
- **FIX** prints a clean unified diff migrating to `PaymentIntent.create`
  (`source` → `payment_method`, `confirm=True`, SCA-safe
  `automatic_payment_methods`) and a contract test sketch

End-to-end proof: `fix --apply` on a copy, then the generated contract test
passes against the patched code (`pytest tests/ -q` → green).

## Tests

```bash
python -m pytest -q        # 34 tests: scanner, analyzer, fixer, deprecations
```

## Project structure

```
src/integration_rot/   scanner.py  deprecations.py  analyzer.py  fixer.py
                       vendor_map.py  models.py  cli.py
data/deprecations.json curated deprecation DB (SAMPLE DATA — verify before prod use)
demo/sample-app/       intentionally-outdated demo target
tests/                 pytest suite
demo.sh                one-command demo
```

## What's stubbed for v2

- **Live feeds**: `RSSChangelogFetcher` / `GitHubReleasesFetcher` raise
  `NotImplementedError` — wire feedparser + an extractor, then register in
  `FETCHERS` per vendor. Analyzer code doesn't change.
- **More fixers**: only `stripe-charges-api` has a drafter; the registry is
  ready for Twilio Verify, SendGrid v3, Plaid `/transactions/sync`, etc.
- **PR opening**: currently prints/applies diffs locally; v2 opens real PRs
  via the GitHub API with the contract test attached.
- **Schema-drift detection**: DB covers deprecations; live response-schema
  diffing against vendor OpenAPI specs is the next module.

## Pushing to GitHub

No credentials are configured in this environment, so the repo is committed
locally and push-ready:

```bash
git remote add origin git@github.com:Kayforkind/integration-rot.git
git push -u origin main
```
