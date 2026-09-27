# Integration-rot launch kit

Demo video: `marketing/demo.mp4` (21s, real CLI output)
Docs + waitlist: https://navigatorslab.com/Integrationrot#waitlist
Repo: https://github.com/Kayforkind/integration-rot

Post in this order: Threads/X/LinkedIn first (own audience), then Show HN
(Tue–Thu morning US time), then Reddit a day later. Reply to every comment
in the first 6 hours — that window decides the ranking.

---

## 1. Hacker News — Show HN

Title: `Show HN: Integration-rot – find deprecated API usage before production does`

Body:

```
Every API you depend on is a liability with a timer on it. Stripe deprecates
an endpoint, Twilio sunsets a product, SendGrid kills v2 — and the blast
radius hides across your repos until something breaks at 2am.

I built integration-rot to catch it early. It scans your dependencies AND
your source code, matches usage against a curated deprecation knowledge base
(17 entries, each linked to the vendor's own announcement — dates only where
the vendor published them), ranks the risk, and drafts the migration as a
patch with a generated contract test.

The part I'm proudest of: the agent loop applies each fix to an isolated copy
of your repo, runs the contract tests, and keeps the fix only if they pass.
Your repo is never modified in place — you get diffs to review.

Honest limits: 8 auto-fixers today (Stripe, SendGrid, Twilio Verify, Plaid,
Slack, Mailchimp...), contract tests are mock-based rather than live vendor
sandboxes, and the DB is hand-curated so coverage is still thin. Zero runtime
dependencies, MIT licensed.

Demo video in the README. If you want a hosted version that watches your
repos continuously and opens the PRs for you, there's a waitlist on the docs
page — it genuinely shapes what gets built next.

Would love the HN treatment: where does this break on your stack?
```

---

## 2. Reddit — r/programming (title) + body

Title: `I built a tool that finds deprecated third-party API usage in your codebase and drafts the migration`

Body:

```
The pattern that keeps biting me: a vendor deprecates something (Stripe's
Charges API, SendGrid v2, Twilio Authy...), the announcement goes to someone's
inbox, and eighteen months later it breaks in production.

So I built integration-rot: it parses your manifests (package.json,
requirements.txt, go.mod, Gemfile, pom.xml), maps 76 SDK packages to vendors,
heuristically finds direct vendor API calls in source, and matches everything
against a deprecation database where every entry links to the vendor's own
source. Then it drafts the migration as a unified diff plus a contract test.

The agent mode is the interesting bit — it applies each fix to a temp copy,
runs pytest contract tests, and only keeps fixes that pass. Nothing touches
your repo until you apply the diff yourself.

It's MIT, zero dependencies, 131 tests. Real limitations: 8 fixers so far,
mock-based contract tests, hand-curated DB (17 entries). Demo video in the
README, 21 seconds.

Curious: what's the worst "we didn't know it was deprecated" outage you've
seen? Trying to figure out which vendors to cover next.
```

(Also works for r/devops, r/selfhosted with the title tweaked toward
self-hosting: "Self-hosted deprecation watcher for your API dependencies".)

---

## 3. X / Twitter — thread

1/ Every API you depend on is a liability with a timer on it.

Stripe deprecates an endpoint. Twilio sunsets a product. SendGrid kills v2.

And the blast radius hides across your repos until it breaks at 2am.

I built something to catch it early. 🧵

2/ integration-rot scans your manifests AND your source, matches usage
against a curated deprecation DB (every entry linked to the vendor's own
announcement), ranks the risk, and drafts the migration as a patch +
contract test.

3/ The agent loop is the part I'm proud of: it applies each fix to an
isolated copy, runs the contract tests, and keeps the fix ONLY if they pass.

Your repo is never touched. You get diffs to review.

4/ Demo (21s, real output): [attach demo.mp4]

5/ MIT, zero runtime deps, 131 tests. Honest limits: 8 fixers today, mock
contract tests, hand-curated DB.

Building a hosted version that watches repos continuously and opens the PRs.
Waitlist's on the docs page — it shapes what gets built next:

navigatorslab.com/Integrationrot#waitlist

---

## 4. LinkedIn

```
Production outages have a quiet cause nobody talks about: deprecated
third-party APIs.

The vendor announces it. The email goes to someone who left. Eighteen months
later, the endpoint you built on stops working at 2am.

I built integration-rot to make this a solved problem instead of a recurring
surprise. It inventories every third-party API dependency across your repos,
matches usage against a sourced deprecation knowledge base, ranks the risk,
drafts the migration — and proves each fix with a contract test before you
ever see the diff.

It's open source (MIT) and the demo takes 21 seconds. For engineering leaders:
this is the class of tooling that turns "we didn't know" into a dashboard.

If continuous monitoring + auto-opened PRs would help your team, the hosted
waitlist is on the docs page. Early signups directly shape the roadmap.
```

---

## 5. Threads (post as-is)

Every API you depend on is a liability with a timer on it. Stripe deprecates
an endpoint, Twilio sunsets a product — and it breaks at 2am, eighteen months
after the announcement email went to someone who left.

I built integration-rot: it scans your code for deprecated third-party API
usage, drafts the migration, and proves each fix with a contract test before
you see the diff. MIT, zero dependencies.

21-second demo + hosted waitlist: navigatorslab.com/Integrationrot
