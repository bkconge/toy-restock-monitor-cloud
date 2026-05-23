# Toy Restock Monitor — Cloud Edition (v2) — Spec rev 3

Changes from rev 1 → rev 2 → rev 3 tracked in §14.

## 1. Goal

Run the v1 restock monitor in GitHub Actions on a ~5-minute cron so it works
24/7 regardless of whether Brian's Mac is awake. Same products, same alert
condition (in stock at ≤ $6.99), but a different deployment surface and a
different notification path because GitHub Actions runners are Linux and
cannot drive `osascript` for iMessage.

This spec inherits all unchanged decisions from v1's
`toy-restock-monitor.md` (rev 3): products, MSRP cap, polite-polling rules,
state schema, cooldown semantics, parser-broken state machine, retention.
Where v1 and v2 differ, v2 wins for this codebase.

## 2. Coexistence with v1

v1 stays running on Brian's Mac unchanged. v2 ships to a separate **public**
GitHub repo (`bkconge/toy-restock-monitor-cloud`) and runs in parallel.
Both publish to the **same ntfy topic** so Brian receives duplicate alerts —
this is a feature, not a bug, for the 1–2 week comparison window:

- If v1 alerts but v2 doesn't: v1's home IP got through where Actions
  didn't (or v2 wasn't ticking at that moment).
- If v2 alerts but v1 doesn't: Mac was asleep when v1 should have caught it.
- If both alert: confirmation; resellers haven't drained it yet.

To distinguish them at a glance, **v2's `NtfyNotifier` prepends `[Cloud] `
to every alert body** before POST (see §5.2). Brian sees `[Cloud] 🟢 IN
STOCK at MSRP — Squeeezy Cheese …` from v2, and the unprefixed body from v1.

Cooldown is per-database, not shared. Up to **4 notifications per restock
event** (1 ntfy + 1 iMessage per system × 2 systems within 30s) is the
worst case. Acknowledged as tolerable for the comparison window; not
worth a cross-system cooldown mechanism.

After 1–2 weeks Brian decides whether to retire v1, retire v2, or keep both.
This spec doesn't pre-commit to that decision.

## 3. Runtime — GitHub Actions cron

`.github/workflows/restock-monitor.yml`.

- **Cron expression:** `cron: '7-59/5 * * * *'` — every 5 min, offset 7
  minutes past the hour. The offset matters: GitHub Actions cron suffers
  worst drift around top-of-hour boundaries (when most workflows in the
  world also fire). Offsetting to `:07, :12, :17, …` dodges the worst
  contention window. Documented behavior, not a hack.
- **Realistic cadence is 10–20 min, NOT 5 min.** GitHub Actions cron is
  best-effort; community reports consistently document 15-min drift as
  common, 30+ min drift on busy days, and occasional skipped runs during
  platform incidents. The spec acknowledges this honestly. Effective
  median cadence ≈ 10 min; p95 ≈ 20 min; tail to 30–60 min.
- **Manual trigger:** `workflow_dispatch` so Brian can fire a test run
  on demand from the Actions UI.
- **Runner:** `ubuntu-latest`. Pure stdlib + PyYAML.
- **Job timeout:** `timeout-minutes: 6`. The orchestrator's internal
  wall-clock cap is **180s** (3 min) to leave ~3 min of headroom for
  Actions setup overhead (checkout, setup-python, pip install, cache
  restore — measured at ~45–75s in practice).
- **Concurrency:** `concurrency.group: restock-monitor` with
  `cancel-in-progress: false`. If a previous tick is still running when
  the next 5-min mark fires, the new tick queues behind it. Actions
  concurrency queue caps at ~100 pending runs, beyond which new runs
  cancel — Brian will never hit this in practice. The orchestrator's
  pidfile/flock from v1 stays as a belt-and-suspenders defense.
- **Branch scoping:** all scheduled runs execute on the `main` branch.
  Actions cache is also branch-scoped, so caches written on `main` are
  visible only to `main` runs. No issue for our use case (we ship from
  `main` only) but worth stating to prevent future "why isn't my feature
  branch picking up the cache?" confusion.

### 3.1 Heartbeat workflow (R7 mitigation)

`.github/workflows/heartbeat.yml` runs weekly and pushes a single-line
commit updating `last-heartbeat.txt`. **Why:** GitHub auto-disables
scheduled workflows on public repos after **60 days of repo inactivity** —
cron runs themselves do NOT count as activity, only commits/issues/PRs do.
Without a heartbeat, Brian's bot silently stops after ~2 months.

- Schedule: `cron: '0 9 * * MON'` (every Monday 09:00 UTC = 2am PT — quiet).
- Action: `echo "$(date -u --iso-8601=seconds)" > last-heartbeat.txt &&
  git add last-heartbeat.txt && git commit -m "heartbeat: $(date -u)" &&
  git push`.
- Uses `${{ secrets.GITHUB_TOKEN }}` with `contents: write` permission.

## 4. State persistence — GitHub Actions cache

Actions runners are ephemeral; nothing local persists between runs. v2 uses
`actions/cache/restore@v4` + `actions/cache/save@v4` (split form) to
round-trip the orchestrator's state.

**Cache key strategy:** stable key `state-v1` (NOT per-run-id).

- **Restore:** `key: state-v1` — exact match, returns the most recent
  save with that key.
- **Save (after tick):** `key: state-v1`. Actions cache does NOT allow
  in-place overwrite of an existing key — the second save with the same
  key would be a no-op. Workaround:
  1. The `Save state cache` step deletes the existing entry first via
     `gh actions-cache delete state-v1 --confirm || true` (idempotent —
     ignores "not found" on first run).
  2. Then `actions/cache/save@v4` writes the fresh state.
  This pattern produces exactly ONE cache entry of name `state-v1` at
  all times. No churn, no 10GB-cap risk.

Cached paths:
- `data/state.db` (SQLite — snapshots, alerts, parse_fail_counters)
- `data/cookies-*.txt` (per-UA cookie jars)
- `data/cookies-*.warmup` (warm-up timestamps)

NOT cached: `data/orchestrator.pid` (per-runner; meaningless across runs),
`logs/*` (workflow logs are GitHub's responsibility).

**Cache eviction:** with the stable-key + delete-then-save pattern, the
cache is touched on every successful save (≥288 times/day). 7-day idle
eviction is never triggered.

## 5. Notification — ntfy only

iMessage cannot be sent from Linux Actions runners. The iOS Shortcut bridge
explored in rev 1 turned out to be impossible — ntfy iOS has no Shortcuts
action that fires on incoming push (open feature request since May 2024),
and iOS Personal Automations triggered by message receipt require a manual
"Run" tap. **v2 sends ntfy and only ntfy.** v1 continues to send iMessage
from Brian's Mac when awake — together they cover the 24/7 surface.

### 5.1 ntfy.sh
- Topic URL via env var `NTFY_TOPIC_URL` from GitHub Secret of the same
  name.
- **Same topic as v1** so Brian gets both v1 and v2 pushes on the same
  subscription.
- Same `NtfyNotifier` as v1, but wrapped in a thin subclass
  `CloudNtfyNotifier` that prepends `[Cloud] ` to every message body
  before POST. v1's bodies are unprefixed.

### 5.2 Notifier config
`config.yaml` ships only one notifier mode:
- `ntfy` (default and only).

The factory in v2 narrows to `{"ntfy"}` and instantiates
`CloudNtfyNotifier`.

### 5.3 Alert format
Same as v1 §5.4, with `[Cloud] ` prefix added by v2 sender:

v2:
```
[Cloud] 🟢 IN STOCK at MSRP — {product_name}
${price} at {retailer}
{short_url}
```

v1 (unchanged, for comparison):
```
🟢 IN STOCK at MSRP — {product_name}
${price} at {retailer}
{short_url}
```

## 6. Configuration

`config.yaml` lives at the repo root, **checked in to a PUBLIC repo**.
No secrets inside; sensitive values are pulled from env vars at load.

```yaml
# config.yaml (in v2 repo)
notifier: ntfy
poll_interval_seconds: 300              # 5 min — matches cron expectation
cooldown_minutes: 60
parse_fail_threshold: 5                 # raised from v1's 3 — datacenter IPs see more transient 403s (R1)
http_read_timeout_seconds: 30
tick_wall_clock_cap_seconds: 180        # leaves room within workflow timeout-minutes:6 for Actions setup
log_level: INFO

# Env-resolved at config.load() — fail-fast on missing var
ntfy_topic_url: env:NTFY_TOPIC_URL
redsky_key: env:TARGET_REDSKY_KEY

watches:
  - id: neeoh-any
    name: "Nee Doh — any variant"
    msrp_cap: 6.99
    sources:
      - https://www.target.com/s?searchTerm=nee+doh

  - id: squeeezy-strawberry
    name: "Squeeezy Strawberry"
    msrp_cap: 6.99
    sources:
      - https://www.target.com/p/sunny-days-squeezy-strawberry/-/A-94757072

  - id: squeeezy-cheese
    name: "Squeeezy Cheese (Target exclusive)"
    msrp_cap: 6.99
    sources:
      - https://www.target.com/p/sunny-days-squeezy-cheese-block/-/A-1003785284
```

### 6.1 `env:` resolver
The config loader recognizes string values starting with `env:` and
substitutes `os.environ[NAME]` at load time. If the env var is unset,
`ConfigError` fires immediately at startup (not at first ntfy send 4
minutes into the tick) with a message naming the missing env var.

### 6.2 Smoke-test override
For AC #4 (verify ntfy delivery works from Actions without depending on
a real restock), the orchestrator honors `TEST_FORCE_IN_STOCK=<watch_id>`:
when set, the matching watch's first source URL is forced to return
`in_stock=True, price=0.01` (well under $6.99 cap) for that one tick.
Triggered manually via `workflow_dispatch` with an input parameter that
the workflow forwards as an env var. No production code path is changed.

**Critical anti-contamination rule (rev 3 fix):** when the override is
active, the orchestrator sends the alert via the notifier BUT skips the
`alerts` table write that would normally happen alongside. This prevents
a smoke test from poisoning the 60-min cooldown for the same watch_id —
otherwise the next genuine restock of e.g. Squeeezy Cheese within an
hour of the smoke would be silently suppressed (exactly the scenario v2
exists to catch). The override path:

1. Build synthetic `StockSnapshot(in_stock=True, price=0.01, sku="forced", …)`.
2. Insert into `snapshots` table normally (so the run summary shows the
   override happened).
3. Format alert message and call `notifier.send(...)`.
4. **Skip** the `INSERT INTO alerts` step that the normal stock-alert
   path uses for cooldown tracking.
5. Continue the tick normally for other watches.

~20 lines of orchestrator code; covered by a unit test that asserts the
synthetic alert fires but no `alerts` row is written.

## 7. Secrets (GitHub repo settings)

`https://github.com/bkconge/toy-restock-monitor-cloud/settings/secrets/actions`:

- `NTFY_TOPIC_URL` — full topic URL. Same as v1's subscription so
  alerts land on the same ntfy app subscription on Brian's phone.
- `TARGET_REDSKY_KEY` — current 40-hex Target public RedSky key.

iMessage handle is NOT a secret in v2 — v2 doesn't send iMessage.
`GH_PAT_FOR_SECRETS` removed from rev 1 — default for RedSky key
rotation is **manual** (see §4 / refresh workflow), not automated.

## 8. Code reuse strategy

v2 is a **fork** of v1's source tree. Concrete delta:

1. Copy `src/` from v1 directory verbatim.
2. **Delete** `src/notify/imessage.py`.
3. **Edit** `src/notify/factory.py`:
   - Remove the `from .imessage import ImessageNotifier` import.
   - Remove the `imessage` and `imessage_mirror_ntfy` branches.
   - Add: `if mode == "ntfy": return CloudNtfyNotifier(config.ntfy_topic_url)`.
   - Single remaining mode: `ntfy`.
4. **Add** `src/notify/cloud_ntfy.py`:
   - `class CloudNtfyNotifier(NtfyNotifier)`: overrides `send(text)` to
     call `super().send(f"[Cloud] {text}")`. ~5 lines.
5. **Edit** `src/config.py` (~20 lines, NOT 5 as rev 1 mistakenly claimed):
   - Drop `imessage_to` field from `Config` dataclass.
   - Drop `imessage_to` from `_REQUIRED_TOP_KEYS`.
   - Drop the constructor binding for it.
   - Narrow `_ALLOWED_NOTIFIERS` to `{"ntfy"}`.
   - Add `_resolve_env(value: str) -> str`: if `value.startswith("env:")`,
     return `os.environ[value[4:]]` or raise `ConfigError` with var name.
   - Wire `_resolve_env` into the load path for `ntfy_topic_url` and
     `redsky_key` (or any value matching the `env:` prefix).
6. **Edit** `src/main.py`: minimal. The existing wiring already calls
   `build_notifier(cfg)` and the factory's signature doesn't change.
   The only edit may be a one-line change to read `redsky_key` from
   config and set the module-level constant on
   `src/sources/_target_key.py` (or pass via env directly to `target.py`
   — implementation detail).
7. **Keep byte-identical:** `src/sources/`, `src/orchestrator.py`,
   `src/db.py`, `src/notify/base.py`, `src/notify/ntfy.py`,
   `src/notify/mirror.py` (unused but harmless to keep).
8. **New:** `.github/workflows/restock-monitor.yml`,
   `.github/workflows/heartbeat.yml`,
   `.github/workflows/refresh-redsky-key.yml`.
9. **New:** `README.md` tailored to the Actions deployment story.
10. **Drop:** `launchd/`, `scripts/install.sh`, `scripts/uninstall.sh`,
    `scripts/verify-launchd.sh` (Mac-only).
11. **Modify:** `scripts/refresh-redsky-key.sh` — keep the scraping
    logic, but instead of writing to `_target_key.py`, **print the new
    key to stdout**. The refresh workflow invokes this script, captures
    the output, and **prints it for Brian to copy** into the
    `TARGET_REDSKY_KEY` GitHub Secret manually via `gh secret set TARGET_REDSKY_KEY`
    from his Mac. Default is manual to keep blast radius small (no PAT
    for secret-write).
12. **Drop:** `scripts/recent-alerts.sh`, `scripts/tail-logs.sh` — use
    `gh run view <id> --log` and `gh run list` instead.
13. **Tests:** drop `tests/test_notify_imessage.py` (-10), prune mirror
    cases from `tests/test_notify_mirror_and_factory.py` (-3 or so), add
    tests for `CloudNtfyNotifier` prefix behavior (+2) and `env:`
    resolver (+3). Net floor: **~55 passing tests** (was 67 in v1).

Acceptance: `git diff` between v1 and v2 should be in the 200–400 LOC
range, dominated by `config.py`, `factory.py`, the new workflow YAMLs,
and the new README.

## 9. Workflow files

### 9.1 `.github/workflows/restock-monitor.yml`

```yaml
name: restock-monitor

on:
  schedule:
    - cron: '7-59/5 * * * *'
  workflow_dispatch:
    inputs:
      force_in_stock:
        description: "watch_id to force in-stock (smoke test); empty for normal run"
        required: false
        default: ""

concurrency:
  group: restock-monitor
  cancel-in-progress: false

permissions:
  contents: read
  actions: write   # for the delete-cache step

jobs:
  tick:
    runs-on: ubuntu-latest
    timeout-minutes: 6
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
          cache: pip

      - run: pip install -r requirements.txt

      - name: Restore state cache
        id: cache-restore
        uses: actions/cache/restore@v4
        with:
          path: data/
          key: state-v1

      - name: Delete previous cache entry (so save can overwrite)
        if: steps.cache-restore.outputs.cache-hit == 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: gh cache delete state-v1 --succeed-on-no-caches

      - name: Run one tick
        env:
          NTFY_TOPIC_URL: ${{ secrets.NTFY_TOPIC_URL }}
          TARGET_REDSKY_KEY:     ${{ secrets.TARGET_REDSKY_KEY }}
          TEST_FORCE_IN_STOCK: ${{ inputs.force_in_stock }}
          PYTHONUNBUFFERED: "1"
        run: python -m src.main --once

      - name: Print summary
        if: always()
        run: |
          python -c "
          import sqlite3
          c = sqlite3.connect('data/state.db')
          print('snapshots last 10 min:', c.execute(\"SELECT COUNT(*) FROM snapshots WHERE fetched_at > datetime('now','-10 minutes')\").fetchone()[0])
          print('alerts last 24h:', c.execute(\"SELECT COUNT(*) FROM alerts WHERE fired_at > datetime('now','-24 hours')\").fetchone()[0])
          "

      - name: Save state cache
        if: always()
        uses: actions/cache/save@v4
        with:
          path: data/
          key: state-v1
```

### 9.2 `.github/workflows/heartbeat.yml`

```yaml
name: heartbeat
on:
  schedule:
    - cron: '0 9 * * MON'
  workflow_dispatch:

permissions:
  contents: write

jobs:
  push-heartbeat:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Update heartbeat file and push
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          date -u --iso-8601=seconds > last-heartbeat.txt
          git add last-heartbeat.txt
          git commit -m "heartbeat: $(date -u --iso-8601=seconds)" || echo "no change"
          git push
```

### 9.3 `.github/workflows/refresh-redsky-key.yml`

```yaml
name: refresh-redsky-key
on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  scrape:
    runs-on: ubuntu-latest
    timeout-minutes: 2
    steps:
      - uses: actions/checkout@v4
      - name: Scrape current RedSky key
        run: bash scripts/refresh-redsky-key.sh
      # Output ends in stdout. Brian copies the key from the workflow log
      # and runs `gh secret set TARGET_REDSKY_KEY` from his Mac.
      - name: Instruction reminder
        run: |
          echo
          echo "Copy the key printed above and run from your Mac:"
          echo "  gh secret set TARGET_REDSKY_KEY --repo bkconge/toy-restock-monitor-cloud"
```

## 10. Acceptance criteria

1. **Workflow file is valid:** `actionlint .github/workflows/*.yml`
   passes (or GitHub UI doesn't reject the files on push).
2. **First scheduled run completes within 30 minutes of merge to `main`**
   (rev 1 said 10 min — corrected for honest cron-drift expectations).
   The run appears under
   `github.com/bkconge/toy-restock-monitor-cloud/actions` with status
   "success" and the `Print summary` step shows ≥3 snapshot rows.
3. **State persists across runs:** the second scheduled run's
   `Restore state cache` step reports `cache-hit: true`, and
   `data/state.db` contains snapshot rows from prior runs.
4. **ntfy delivery from Actions works:** trigger the workflow manually
   with `force_in_stock=squeeezy-cheese`. Within ~1 min an `[Cloud] 🟢 IN
   STOCK at MSRP …` push arrives on Brian's iPhone via the ntfy app.
5. **Env-var-missing fails fast:** remove the `TARGET_REDSKY_KEY` secret
   temporarily and trigger `workflow_dispatch`. The tick exits non-zero
   within ~5s of `python -m src.main --once` start; stderr names
   `TARGET_REDSKY_KEY`.
6. **No secrets in workflow logs:** `gh run view <id> --log | grep -F
   "$(gh secret list)"` returns no plaintext matches for the secret
   values. (Sanity check, not a security boundary — GitHub's masking
   is the actual defense.)
7. **Local test suite passes:** `python -m unittest discover tests/`
   ≥ 55 tests, all green.
8. **Coexistence works:** v1 still ticking on Mac (`make recent-alerts`
   on v1 shows recent activity). v2 ticking in Actions (`gh run list
   --workflow restock-monitor --limit 5` shows recent successful runs).
   Both publish to the same ntfy topic — verifiable by comparing v1's
   `config.yaml` `ntfy_topic_url` against v2's `NTFY_TOPIC_URL` secret
   value.
9. **Heartbeat workflow ran in the last 7 days:** `gh run list
   --workflow heartbeat --limit 1` shows a recent success and the
   `last-heartbeat.txt` file commit is < 8 days old. (Prevents
   60-day auto-disable, R7.)
10. **Refresh-key workflow prints a parseable key:** trigger
    `workflow_dispatch` on `refresh-redsky-key`; log contains a 40-hex
    string and the manual-copy instruction.

## 11. Out of scope (v2)

- iMessage sent from process. Not possible from Linux Actions; iOS
  Shortcut bridge is not a real capability (memory:
  `ntfy-ios-no-shortcut-trigger`).
- SMS fallback / Pushover / email-to-SMS-gateway alternatives.
- Web dashboard.
- Reseller markup tracking (eBay/StockX).
- Multiple recipients.
- Walmart, Schylling, Sunny Days sources — bot-detection issues from v1
  apply more harshly from datacenter IPs.
- chat.db delivery confirmation (Mac-only).
- Sub-5-min cadence.
- Automated RedSky key rotation (manual path is the default; revisit if
  rotation cadence becomes annoying).
- Cross-system cooldown (would require a shared backend; not worth it
  for the comparison window).

## 12. Risks & open questions

- **R1 — Target bot-blocks GitHub Actions IPs.** Datacenter IPs
  (Azure-hosted `ubuntu-latest` runners) face significantly higher
  Akamai/WAF challenge rates than residential IPs. Realistic
  expectation: **5–20% of RedSky calls return 403 / challenge** from
  Actions vs near-zero from Brian's home IP. Mitigation: `parse_fail_threshold`
  raised to 5 in `config.yaml` (vs v1's 3) so transient 403s don't
  generate noisy `parser_broken` alerts. Cookie warm-up + UA rotation
  (inherited from v1's `HttpClient`) still applies. If RedSky failure
  rate exceeds 20% sustained for a week, follow-up scope is a
  residential-proxy pass (v3).
- **R2 — Cache eviction edge case.** Stable-key + delete-then-save
  pattern (§4) keeps exactly one cache entry. Multi-hour GitHub outage
  could fail enough runs that the cache goes 7+ days untouched →
  evicted. On next successful run, restore is a miss, orchestrator
  bootstraps fresh state. Acceptable — short loss of cooldown history.
- **R3 — Q1: PAT for automated key rotation.** Default is manual
  rotation. Brian gets the new key printed in the
  `refresh-redsky-key.yml` workflow log and runs `gh secret set
  TARGET_REDSKY_KEY` from his Mac. Manual path keeps blast radius small.
- **R4 — ntfy iOS delivery reliability.** Same as v1 R3. v2 doesn't
  add a new risk here; the same ntfy.sh / iOS app is the delivery
  surface. v1's iMessage parity (when Mac awake) provides redundancy
  during waking hours.
- **R5 — Public repo abuse.** Anyone can read the code and watch the
  topic URLs (the watched product URLs are public Target PDPs).
  Secrets are repo-scoped and DO NOT follow forks. Worst-case threat:
  someone with the ntfy topic URL could spam it with junk pushes;
  Brian's phone would buzz. Mitigation: ntfy topic name has 12-hex
  random suffix making it un-guessable; the topic appears in the
  Secret value (not in source) so it doesn't leak via commits.
- **R6 — Duplicate cooldown across v1 + v2.** Up to 4 alerts per
  restock within 30s (1 ntfy + 1 iMessage per system × 2 systems).
  Acknowledged as tolerable for the 1–2 week comparison window. If
  it gets annoying, retire one system.
- **R7 — Public repo scheduled-workflow 60-day auto-disable.** GitHub
  auto-disables scheduled workflows on public repos after 60 days of
  repo inactivity (cron firing does NOT count; only commits/issues/PRs
  do). Mitigated by the heartbeat workflow (§3.1) that commits weekly.
- **R8 — Cron drift.** Effective median cadence is ~10 min, p95 ~20 min,
  tail to 30–60 min on busy GitHub days. Acceptable given v2's job is
  filling overnight Mac-asleep gaps — even a 30-min cadence overnight is
  better than zero coverage.
- **Q1 — ~~RESOLVED~~** default to manual key rotation; no PAT needed.

## 13. Workflow this spec is feeding

1. ✅ Spec drafted (rev 1).
2. ✅ Dual review of spec (engineering + operational lenses).
3. ✅ Spec revisions applied (this file — rev 2).
4. → `/plan` against rev 2.
5. → Dual review of plan.
6. → Apply plan revisions.
7. → `/plan_w_team`.
8. → `/cook`.

## 14. Changelog

**rev 3 (post plan-review fixes)** — targeted corrections surfaced by
plan-review of plan rev 1:
- §6.2 — TEST_FORCE_IN_STOCK now SKIPS the `alerts` table write to
  prevent contamination of real cooldown state for the same watch_id.
  Previously the smoke could silently suppress a real restock for 60min.
- §7, §9.1 — `REDSKY_KEY` renamed to `TARGET_REDSKY_KEY` to align with
  v1's existing env-var convention in `src/sources/_target_key.py`. No
  v2 code rename needed; only the secret name and workflow env block.
- §9.1 — `gh actions-cache delete state-v1 --confirm` (invalid command,
  would have failed every cache-saving tick) → `gh cache delete state-v1
  --succeed-on-no-caches` (modern, correct).

**rev 2 (post dual-review)** — major:
- **Dropped iOS Shortcut bridge entirely** — verified via web research
  that ntfy iOS has no Shortcut trigger (open feature request since
  May 2024) and iOS can't silently push-trigger Shortcuts. v2 sends
  ntfy only; v1 keeps doing iMessage from the Mac when awake.
- **Added `[Cloud] ` prefix** to v2's `CloudNtfyNotifier` so Brian can
  distinguish v1 vs v2 alerts on the same topic.
- **Added heartbeat workflow** (R7) — public-repo scheduled workflows
  auto-disable after 60 days of repo inactivity; weekly commit keeps it
  alive.
- **Honest cron cadence** in §3 — 10–20 min effective, not 5–15.
  AC #2 latency window changed 10 min → 30 min.
- **Cron offset to `7-59/5`** to dodge top-of-hour drift.
- **`parse_fail_threshold` raised to 5** in v2 config (was 3 in v1)
  to account for datacenter-IP 403 rate from Target's WAF (R1).
- **Cache key strategy:** stable `state-v1` with explicit
  delete-then-save pattern (replaces the unbounded-key scheme from rev 1
  which would have blown the 10GB cap in weeks).
- **`tick_wall_clock_cap_seconds` lowered to 180s** (workflow
  `timeout-minutes: 6` leaves ~3 min for Actions setup + delete-cache).
- **Q1 defaulted to manual** RedSky key rotation; `GH_PAT_FOR_SECRETS`
  removed from §7. Refresh workflow prints key to log; Brian copies and
  runs `gh secret set` from his Mac.
- **`TEST_FORCE_IN_STOCK` env hook** added to orchestrator for AC #4
  smoke testing (replaces rev 1's hand-wavy "synthetic test").
- **`env:` resolver explicitly fail-fast at startup** (AC #5).
- **Code-reuse delta corrected**: ~20 lines not 5; test floor ~55 not
  67+; `main.py` essentially unchanged; `refresh-redsky-key.sh`
  rewritten to print-only.
- **Branch scoping note** added to §3.

**rev 1** — initial draft.
