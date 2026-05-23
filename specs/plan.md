# Plan: Toy Restock Monitor Cloud (v2) — Build Plan rev 2 (post plan-review)

> Implementation plan for spec `toy-restock-monitor-cloud.md` rev 3.
> Task type: **feature (cloud-deployment fork)** · Complexity: **medium-low**.
> Rev 2 incorporates all Critical + Important findings from the dual review
> of plan rev 1. Changes tracked in §Changelog.

## Task Description

Fork v1 source from `/Users/briancongelliere/claude/projects/toy-restock-monitor`
to `/Users/briancongelliere/claude/projects/toy-restock-monitor-cloud`,
make v2-specific edits per spec §8, write three GitHub Actions workflow
files, then create + push to public repo `bkconge/toy-restock-monitor-cloud`
and verify the first scheduled run.

v1 (Mac launchd) and v2 (Actions cron) run alongside each other publishing
to the same ntfy topic for a 1–2 week comparison window. v2 alerts carry a
`[Cloud] ` prefix to distinguish from v1's unprefixed messages.

## Objective

All 10 acceptance criteria from spec rev 3 §10 PASS. Local checkpoints are
green; Actions cron is ticking on the public repo; the TEST_FORCE_IN_STOCK
smoke triggers a `[Cloud]`-prefixed ntfy push on Brian's iPhone without
poisoning the production cooldown.

## Solution Approach

Three phases, twelve sequential tasks plus 11 deploy/verify steps:

1. **Foundation (local)** — fork v1 source, edit `config.py` for
   env-resolver + drop iMessage, edit factory + delete imessage.py + add
   CloudNtfyNotifier, update tests, update `config.example.yaml` to v2
   shape. Phase 1 done when `make test` is green with ~55 tests.
2. **Wiring (local)** — add TEST_FORCE_IN_STOCK orchestrator hook
   (with cooldown-bypass semantic from spec §6.2), write the three
   workflow YAMLs (validated via `actionlint`), modify
   `scripts/refresh-redsky-key.sh` to print-only AND remove its
   `.venv/bin/python` dependency for Linux portability, write README.
3. **Deploy + verify** — Brian-present interactive steps: repo create,
   secrets set, push, first run, cache hit, smoke test, heartbeat
   trigger, refresh-key trigger, env-missing-fails-fast (last so it
   can't poison Phase 3 cache verification).

## Relevant Files

- `specs/toy-restock-monitor-cloud.md` (rev 3) — locked spec.
- `/Users/briancongelliere/claude/projects/toy-restock-monitor/src/**` — v1
  source forked verbatim where possible.
- `/Users/briancongelliere/claude/projects/toy-restock-monitor/tests/**` — v1
  tests forked with edits per §8.
- `/Users/briancongelliere/claude/projects/toy-restock-monitor/scripts/refresh-redsky-key.sh`
  — forked, then modified (print-only + drop `.venv` dependency).
- `/Users/briancongelliere/claude/projects/toy-restock-monitor/config.yaml`
  — local, gitignored; read for `ntfy_topic_url` value during deploy.
- `/Users/briancongelliere/claude/projects/toy-restock-monitor/src/sources/_target_key.py`
  — local; read `_DEFAULT_REDSKY_KEY` literal during deploy.
- `~/claude/CLAUDE.md` — Brian's global rules (memory:
  `git-config-never-global`).

## Step by Step Tasks

IMPORTANT: Execute in order. Each ends with a checkpoint. Tests via
`make test`.

### 1. Fork v1 source tree + venv (Phase 1)
- `cd /Users/briancongelliere/claude/projects/toy-restock-monitor-cloud`
- Fork verbatim from v1:
  ```bash
  rsync -av --exclude .venv --exclude data --exclude logs --exclude config.yaml \
        ../toy-restock-monitor/{src,tests,scripts,requirements.txt,config.example.yaml,.gitignore} .
  ```
- Delete Mac-only files: `rm -rf launchd/ scripts/install.sh
  scripts/uninstall.sh scripts/verify-launchd.sh scripts/_smoke_test.py
  scripts/recent-alerts.sh scripts/tail-logs.sh`
- Create venv:
  `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
- **HARD-STOP CHECKPOINT:** `.venv/bin/python -m unittest discover tests/`
  reports exactly `67/67` tests passing. If count differs, the fork is
  corrupt — STOP, don't proceed.

### 2. Rewrite config.example.yaml to v2 shape (Phase 1)
v1's `config.example.yaml` ships with `notifier: imessage_mirror_ntfy`,
`imessage_to: "+18058077080"`, and `per_source_overrides`. v2 needs:
```yaml
notifier: ntfy
poll_interval_seconds: 300
cooldown_minutes: 60
parse_fail_threshold: 5                 # raised for v2 datacenter-IP reality
http_read_timeout_seconds: 30
tick_wall_clock_cap_seconds: 180
log_level: INFO

ntfy_topic_url: env:NTFY_TOPIC_URL
redsky_key: env:TARGET_REDSKY_KEY       # name aligned to v1 env var

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

Drop the `imessage_to` line. Drop the `per_source_overrides` block (Actions
cron is the limiter; no per-URL overrides). **Checkpoint:** `yaml.safe_load`
succeeds on the file.

### 3. Edit src/config.py — drop iMessage, add env: resolver, add redsky_key (Phase 1)

References below assume v1 line numbers (from reviewer cross-check).

- **Drop `imessage_to`** from:
  - `Config` dataclass (line 33-ish).
  - `_REQUIRED_TOP_KEYS` (line 47).
  - Type validation map (line 65).
  - Constructor binding (around line 142).
- **Add `redsky_key: str`** to:
  - `Config` dataclass.
  - `_REQUIRED_TOP_KEYS` with type `str`.
  - Constructor binding.
- **Narrow `_ALLOWED_NOTIFIERS`** to `{"ntfy"}` (remove `imessage` and
  `imessage_mirror_ntfy`).
- **Add `_resolve_env(value: str) -> str`** helper:
  ```python
  def _resolve_env(value: str) -> str:
      if not isinstance(value, str) or not value.startswith("env:"):
          return value
      name = value[4:]
      if not name:
          raise ConfigError("config: 'env:' prefix without variable name")
      try:
          return os.environ[name]
      except KeyError:
          raise ConfigError(f"config: env var {name!r} is not set") from None
  ```
- Wire into the load path: after YAML parse, walk top-level scalar
  string values and substitute via `_resolve_env`. (Watches list need
  not be walked — their fields are URLs, not env: values.)
- **Leave `per_source_overrides` validation as-is** — defaults to `{}`
  when absent; harmless to retain.

### 4. Edit tests/test_config.py — drop iMessage assertions, add env: tests (Phase 1)
- Strip any test referencing `imessage_to`.
- Drop the test that asserts `imessage_mirror_ntfy` is a valid notifier
  mode.
- Add tests for `_resolve_env`:
  - Happy path: `ntfy_topic_url: env:NTFY_TOPIC_URL` with var set →
    resolved value appears in `Config.ntfy_topic_url`.
  - Missing env var → `ConfigError` with the env var name in message.
  - `env:` (empty name) → `ConfigError`.
  - Non-env string passes through unchanged.
- Add a test that `_ALLOWED_NOTIFIERS == {"ntfy"}` (or equivalent —
  assert `imessage` raises `ConfigError` from `load`).
- **Checkpoint:** `make test` shows `test_config` green with the new
  assertions; other test files may still fail (expected).

### 5. Edit factory.py + delete imessage.py + add CloudNtfyNotifier (Phase 1)
- Remove `from .imessage import ImessageNotifier` import.
- Single remaining `build_notifier` branch:
  ```python
  if mode == "ntfy":
      return CloudNtfyNotifier(config.ntfy_topic_url)
  raise ConfigError(f"unknown notifier mode: {mode!r}")
  ```
- Create `src/notify/cloud_ntfy.py`:
  ```python
  from .ntfy import NtfyNotifier

  class CloudNtfyNotifier(NtfyNotifier):
      def send(self, text):
          return super().send(f"[Cloud] {text}")
  ```
- `rm src/notify/imessage.py`.
- `src/notify/__init__.py` is 0 bytes in v1 — no edit needed (reviewer
  confirmed).
- **Checkpoint:** `.venv/bin/python -c "from src.notify.factory import
  build_notifier"` imports cleanly.

### 6. Rewrite tests/test_notify_mirror_and_factory.py + add CloudNtfyNotifier tests (Phase 1)

**Critical:** the v1 file has `from src.notify.imessage import
ImessageNotifier` at module top — after task 5 deletes imessage.py, this
file ImportErrors at collection. Two paths chosen by spec §8:

- **Drop mirror tests entirely** (mirror class is retained but unused in
  v2). Rewrite the file to test ONLY the factory:
  - `notifier: ntfy` → returns `CloudNtfyNotifier`.
  - Unknown mode → `ConfigError` with bad value in message.
  - Drop everything else; the file goes from ~120 lines to ~30.
- `rm tests/test_notify_imessage.py` (10 tests gone).
- Create `tests/test_notify_cloud_ntfy.py`:
  - `CloudNtfyNotifier("http://test").send("hello")` — mock
    `urllib.request.urlopen`; inspect the POST body; assert `[Cloud] hello`.
  - Prefix is always added even if input already starts with `[Cloud]`
    (spec defines no dedup).
- **Checkpoint:** `make test` shows `test_notify_*` green; total count
  approximately **55** (was 67; -10 imessage, -~5 mirror modes,
  +~2 cloud_ntfy, +~3 env resolver = -10 net).

### 7. Update tests/test_main_bootstrap.py heredoc YAML fixtures (Phase 1)

v1's `test_main_bootstrap.py` lines 13–14 and 27–28 use
`notifier: imessage_mirror_ntfy` and `imessage_to: "+18058077080"` in
inline heredoc YAML fixtures used for the subprocess tests. Both fixtures
must be rewritten to v2-shape (drop `imessage_to`, set
`notifier: ntfy`, add `redsky_key: env:TARGET_REDSKY_KEY`).

- Rewrite both YAML heredocs to v2 shape.
- Drop any assertion mentioning `imessage_to`.
- **Add** an env-var-missing test: write a config with
  `ntfy_topic_url: env:MISSING_NTFY` and run `main.py --once` in a
  subprocess with `MISSING_NTFY` unset. Assert exit ≠ 0 and stderr
  contains `MISSING_NTFY`.
- **Checkpoint:** `make test` fully green. **Phase 1 done.**

### 8. Add TEST_FORCE_IN_STOCK orchestrator hook + tests (Phase 2)

**Insertion point** (per spec §6.2 + reviewer correction): AFTER
`_insert_snapshot` and AFTER the parse-fail counter reset, but BEFORE
the cooldown check. This prevents the override from clobbering legitimate
counter state.

```python
forced = os.environ.get("TEST_FORCE_IN_STOCK", "").strip()
if forced and watch.id == forced:
    snapshot = StockSnapshot(
        in_stock=True, price=0.01, title="TEST_FORCE_IN_STOCK override",
        sku="forced", http_status=200, error=None, parse_error=False,
    )
    self._insert_snapshot(...)              # synthetic snapshot row recorded
    msg = _format_stock_alert(watch, snapshot, source_url)
    self.notifier.send(msg) if not dry_run else _write_dryrun(msg)
    # CRITICAL: skip alerts table write — prevents 60-min cooldown
    # contamination of real Squeeezy Cheese restocks within the next hour
    log.warning("TEST_FORCE_IN_STOCK fired for %s; alerts row NOT written", watch.id)
    continue  # to next source_url; skip the normal stock-alert path
```

Add tests to `test_orchestrator.py`:
- Test 1: with `TEST_FORCE_IN_STOCK=squeeezy-cheese`, OOS fake source,
  run one tick. Assert `notifier.calls == 1`, message starts with
  `🟢 IN STOCK`, **`alerts` table has 0 rows for `kind='stock'`**, and a
  `snapshots` row with `sku='forced'` was inserted.
- Test 2: with no env var, no override; OOS behavior unchanged.
- Test 3: with override AND a real in-stock fake source for the SAME
  watch_id, only ONE notifier call (the override path wins; the normal
  path is skipped via `continue`).
- Test 4: Re-firing TEST_FORCE_IN_STOCK within 60 min — both fires send
  notifications (no cooldown row blocking).
- **Checkpoint:** `make test` green; orchestrator test count +4.

### 9. Write workflow files (Phase 2)

Precondition: `command -v actionlint >/dev/null 2>&1 || brew install
actionlint`. Add to README "operations" section as a one-time setup.

- Write `.github/workflows/restock-monitor.yml` per spec §9.1 verbatim
  (already corrected to use `gh cache delete state-v1
  --succeed-on-no-caches` and `TARGET_REDSKY_KEY` env var).
- Write `.github/workflows/heartbeat.yml` per spec §9.2.
- Write `.github/workflows/refresh-redsky-key.yml` per spec §9.3.
- Validate each: `actionlint .github/workflows/*.yml` exits 0.
- **Checkpoint:** actionlint clean.

### 10. Modify scripts/refresh-redsky-key.sh — print-only + Linux-portable (Phase 2)

v1's script ends with a `.venv/bin/python` heredoc that rewrites
`_target_key.py`. v2 must:

- **Remove the Python heredoc rewrite block** entirely (no `_target_key.py`
  rewrite, no `.venv` dependency).
- Final lines just `echo "$NEW_KEY"` to stdout.
- Add an `echo` line printing the copy-paste reminder:
  ```
  Copy the key above and run from your Mac:
    gh secret set TARGET_REDSKY_KEY --body "<key>" --repo bkconge/toy-restock-monitor-cloud
  ```
- The `grep -oE` / `awk` / `sort | uniq -c` calls work the same on Linux
  GNU coreutils — no portability fixes needed there.
- **Checkpoint locally:** `bash scripts/refresh-redsky-key.sh` runs and
  prints a 40-hex key.

### 11. Write README.md (Phase 2)

Sections (concise but complete):

1. **What it does** — one paragraph; mention v1 coexistence.
2. **Setup precondition** — `brew install actionlint` for local lint;
   `gh auth status` should show bkconge.
3. **Quickstart (fresh clone)**:
   ```bash
   git clone https://github.com/bkconge/toy-restock-monitor-cloud
   cd toy-restock-monitor-cloud
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   make test
   actionlint .github/workflows/*.yml
   ```
4. **Deploy steps** — `gh repo create --public` (no `--confirm` —
   reviewer confirmed that flag doesn't exist), set the two secrets
   (`NTFY_TOPIC_URL`, `TARGET_REDSKY_KEY`), push.
5. **How alerts work** — ntfy only; v1 still sends iMessage from Mac
   when awake; `[Cloud] ` prefix distinguishes.
6. **Smoke test** —
   `gh workflow run restock-monitor.yml -f force_in_stock=squeeezy-cheese`.
   Explain that the override skips the `alerts` table write so it does
   not poison real cooldowns.
7. **Operations** — `gh run list`, `gh run view <id> --log`,
   `make test`, `make lint-workflows`.
8. **Refresh RedSky key** —
   `gh workflow run refresh-redsky-key.yml`, read log for new key,
   `gh secret set TARGET_REDSKY_KEY --body "<key>" --repo
   bkconge/toy-restock-monitor-cloud`.
9. **Troubleshooting** — workflow auto-disabled (60-day inactivity:
   heartbeat workflow prevents; re-enable in Actions UI if it ever
   happens), persistent 403s (raise `parse_fail_threshold`), no pushes
   (check `NTFY_TOPIC_URL` secret, ntfy app subscription, GitHub
   "Workflow permissions" set to read-and-write if heartbeat push 403s).
10. **Coexistence with v1** — `[Cloud]` prefix; duplicate alerts within
    ~30s expected for the 1–2 week comparison window.

Add `Makefile`:
```makefile
test:
	.venv/bin/python -m unittest discover tests/

lint-workflows:
	@command -v actionlint >/dev/null || { echo "install actionlint: brew install actionlint"; exit 1; }
	actionlint .github/workflows/*.yml
```

- **Checkpoint:** `make test` and `make lint-workflows` both pass.
  **Phase 2 done.**

### 12. Pre-flight checks (Phase 3 prep)
- `gh auth status` → confirm logged in as `bkconge`.
- `make test` green.
- `actionlint .github/workflows/*.yml` clean.
- Note Brian's current ntfy topic URL: `grep ntfy_topic_url
  ../toy-restock-monitor/config.yaml` (local file; gitignored in v1).
- Note v1's current REDSKY key: `grep _DEFAULT_REDSKY_KEY
  ../toy-restock-monitor/src/sources/_target_key.py`.
- **Checkpoint:** all values noted; no `gh repo create` yet.

### 13. Create public GitHub repo (Phase 3)
```bash
gh repo create bkconge/toy-restock-monitor-cloud --public \
  --description "24/7 cloud companion to toy-restock-monitor; ntfy alerts via GitHub Actions"
```
(No `--confirm` flag — reviewer confirmed it doesn't exist in current `gh`.)
- **Checkpoint:** `gh repo view bkconge/toy-restock-monitor-cloud`
  succeeds.

### 14. Set GitHub Secrets (Phase 3)
```bash
NTFY=$(grep ntfy_topic_url ../toy-restock-monitor/config.yaml | sed 's/.*"\(.*\)".*/\1/')
KEY=$(grep _DEFAULT_REDSKY_KEY ../toy-restock-monitor/src/sources/_target_key.py | sed 's/.*"\(.*\)".*/\1/')
gh secret set NTFY_TOPIC_URL    --body "$NTFY" --repo bkconge/toy-restock-monitor-cloud
gh secret set TARGET_REDSKY_KEY --body "$KEY"  --repo bkconge/toy-restock-monitor-cloud
gh secret list --repo bkconge/toy-restock-monitor-cloud
```
- **Checkpoint:** `gh secret list` shows both names (values masked).

### 15. Initial commit + push (Phase 3)

Per memory `git-config-never-global`: identity goes in the LOCAL repo,
never `--global`.

```bash
git init
git config user.name  "Brian Congelliere"
git config user.email "<brian's bkconge email>"  # confirm with Brian
git remote add origin https://github.com/bkconge/toy-restock-monitor-cloud.git
git add -A
git status   # review — confirm no .venv/, no data/, no logs/
git commit -m "Initial v2 cloud edition — fork of v1 with ntfy-only notifier + GitHub Actions cron"
git branch -M main
git push -u origin main
```

**Pause for Brian:** confirm the email value before committing.

- **Checkpoint:** repo on GitHub shows the code; Actions tab shows the
  three workflows registered.

### 16. Watch first scheduled run (Phase 3)

**Honest expectation per spec §3 + reviewer:** first fire on a brand-new
repo can take **15–60 min**, not 5. If no run appears within 60 min:
- Push a trivial commit to main to force schedule resync:
  `echo "$(date)" > .last-sync && git add .last-sync && git commit -m
  "force schedule resync" && git push`.
- Wait another 15 min.

Once a run appears:
- `gh run list --workflow restock-monitor.yml --limit 1`.
- `gh run watch <id>` to follow.
- Verify `Print summary` step shows ≥3 snapshot rows.

**Also verify v1 still ticking (AC #8 coexistence):** in another
terminal:
```bash
cd ../toy-restock-monitor
sqlite3 data/state.db "SELECT MAX(fetched_at) FROM snapshots;"
# expect a row from < 10 min ago (v1 launchd is 60s cadence)
launchctl list | grep toy-restock-monitor  # confirm loaded
```

**Verify no secrets in logs (AC #6):**
```bash
TOPIC_SUFFIX=$(echo "$NTFY" | sed 's|.*/||')  # the random suffix only
gh run view <run-id> --log | grep -F "$TOPIC_SUFFIX" && echo "LEAK" || echo "OK"
```

- **Checkpoint:** first run successful + ≥3 snapshots + v1 still
  ticking + no secret leak.

### 17. Verify cache hit on second run (Phase 3)
- Wait for the next `:NN` 5-min slot (or trigger `gh workflow run
  restock-monitor.yml`).
- `gh run view <second-run-id> --log | grep -A 2 "Restore state cache"`
  — expect `cache-hit: true`.
- **Checkpoint:** second run's restore step reports cache-hit: true.

### 18. Smoke test via workflow_dispatch (Phase 3)

**Workflow_dispatch queue note (reviewer):** if a scheduled run is
in flight, the manual dispatch queues FIFO behind it. Expected delay
up to ~5 min.

```bash
gh workflow run restock-monitor.yml -f force_in_stock=squeeezy-cheese
sleep 10
gh run watch $(gh run list --workflow restock-monitor.yml --limit 1 --json databaseId -q '.[0].databaseId')
```

**On Brian's iPhone within ~1 min of the run starting:** an ntfy push
with body starting `[Cloud] 🟢 IN STOCK at MSRP — Squeeezy Cheese …`.

**Brian confirms receipt.** (This checkpoint is unobservable to a
validator agent — mark PASS only when Brian replies "got it.")

- **Checkpoint:** Brian confirms `[Cloud]` push arrived.
- **Sanity (anti-contamination):** after the smoke,
  `gh run view <id> --log | grep "alerts row NOT written"` — confirm
  the log shows the cooldown bypass actually fired.

### 19. Verify heartbeat workflow (Phase 3)
- `gh workflow run heartbeat.yml`.
- `gh run watch <id>`.
- Expect exit 0 and a new commit on `main`:
  ```bash
  gh api repos/bkconge/toy-restock-monitor-cloud/commits | jq '.[0].commit.message'
  # expect "heartbeat: <iso8601>"
  ```
- **If push step fails with 403:** GitHub repo Settings → Actions →
  General → Workflow permissions → "Read and write permissions". Brian
  toggles this once; re-run the workflow.
- **Checkpoint:** heartbeat commit visible; `last-heartbeat.txt`
  exists on `main`.

### 20. Verify refresh-redsky-key workflow (Phase 3)
- `gh workflow run refresh-redsky-key.yml`.
- `gh run view <id> --log | grep -E "^[0-9a-f]{40}$"` — expect at least
  one 40-hex line.
- Brian does NOT need to rotate the secret here; just confirm the
  workflow extracts a key.
- **Checkpoint:** workflow log contains a 40-hex string.

### 21. Env-missing-fails-fast test — LAST so it doesn't poison Phase 3 (Phase 3)

**Reviewer ordering fix:** this is the final task in Phase 3 because
deleting a secret could affect any subsequent scheduled run. By placing
it last, no later task depends on the cache populated by the failed run.

**Tightened wording for "restore immediately":** target restoration
within 60 seconds. Use this one-liner:

```bash
# Note the current time vs next cron slot (every :07, :12, :17, ...).
# If < 2 min to next slot, wait through one tick first.

KEY=$(grep _DEFAULT_REDSKY_KEY ../toy-restock-monitor/src/sources/_target_key.py | sed 's/.*"\(.*\)".*/\1/')
gh secret delete TARGET_REDSKY_KEY --repo bkconge/toy-restock-monitor-cloud && \
  gh workflow run restock-monitor.yml && \
  sleep 30 && \
  gh run view $(gh run list --workflow restock-monitor.yml --limit 1 --json databaseId -q '.[0].databaseId') --log | tail -20 && \
  gh secret set TARGET_REDSKY_KEY --body "$KEY" --repo bkconge/toy-restock-monitor-cloud
```

This:
1. Captures the current key value first.
2. Deletes the secret.
3. Triggers a manual run.
4. Waits 30s then dumps the last 20 log lines (should show ConfigError +
   `TARGET_REDSKY_KEY`).
5. **Immediately restores the secret** before the next scheduled tick.

**Checkpoint:** the log dump shows the env-missing failure mode AND the
final `gh secret set` step succeeded (secret restored).

## Testing Strategy

- **Local unit tests** (`tests/`): ~55 tests. Fixtures carried from v1.
  Mocks at HTTP/subprocess boundaries. No live HTTP in tests.
- **Manual integration tests** (Brian present):
  - Task 16: first scheduled run successful AND v1 coexistence verified.
  - Task 17: cache hit on second run.
  - Task 18: TEST_FORCE_IN_STOCK smoke (Brian-only confirmation).
  - Task 19: heartbeat commits.
  - Task 20: refresh-key workflow extracts a key.
  - Task 21: env-missing-fails-fast (LAST — poison-safe ordering).
- **No test depends on a real restock** — TEST_FORCE_IN_STOCK is the
  test hook.

## Acceptance Criteria Mapping

Mirrors spec rev 3 §10 #1–#10. Plan completion = all 10 PASS.

| # | Criterion                              | Verified at task |
|---|----------------------------------------|------------------|
| 1 | Workflow YAML valid (actionlint)       | 9                |
| 2 | First scheduled run within 30 min, ≥3 snapshot rows | 16 |
| 3 | Second run reports cache-hit: true     | 17               |
| 4 | TEST_FORCE_IN_STOCK delivers `[Cloud]` push | 18 (Brian-only) |
| 5 | Env-var-missing fails fast             | 21 (last)        |
| 6 | No secrets in workflow logs            | 16 (explicit grep) |
| 7 | Local test suite ≥55 green             | 7 (Phase 1 done) |
| 8 | Coexistence — v1 still ticks, v2 ticks, same topic | 16 (explicit launchctl + sqlite check) |
| 9 | Heartbeat workflow ran in last 7 days  | 19               |
| 10 | Refresh-key workflow prints parseable key | 20            |

## Validation Commands

```bash
# Phase 1 done (after task 7):
make test                                     # ~55 green
NTFY_TOPIC_URL=https://ntfy.sh/test TARGET_REDSKY_KEY=$(printf 'a%.0s' {1..40}) \
  .venv/bin/python -c "from src.notify.factory import build_notifier; from src.config import load; print(type(build_notifier(load('config.example.yaml'))).__name__)"
# expect: CloudNtfyNotifier

# Phase 2 done (after task 11):
make test
make lint-workflows                           # actionlint clean

# Phase 3 done (after task 21):
gh run list --workflow restock-monitor.yml --limit 5  # multiple successes
gh run list --workflow heartbeat.yml --limit 1        # one success
gh run list --workflow refresh-redsky-key.yml --limit 1  # one success
gh secret list --repo bkconge/toy-restock-monitor-cloud  # both secrets restored
sqlite3 ../toy-restock-monitor/data/state.db "SELECT MAX(fetched_at) FROM snapshots;"  # v1 still ticking
# Brian confirms [Cloud] push delivered (task 18)
```

## Dependency Graph

```
1 (fork + venv) ─ HARD CHECKPOINT: 67/67 from v1
 └─> 2 (config.example.yaml to v2 shape)
      └─> 3 (config.py edits: drop iMessage, add env: resolver, add redsky_key)
           └─> 4 (test_config.py edits + env: tests)
                └─> 5 (factory.py + delete imessage.py + CloudNtfyNotifier)
                     └─> 6 (test_notify_mirror_and_factory.py rewrite + new cloud_ntfy tests)
                          └─> 7 (test_main_bootstrap.py heredoc rewrites + env-missing test)
                               ── Phase 1 done (AC #7)
                                   └─> 8 (TEST_FORCE_IN_STOCK + orchestrator tests)
                                        ├─> 9 (3 workflow YAMLs + actionlint)
                                        ├─> 10 (refresh-redsky-key.sh print-only + Linux portability)
                                        └─> 11 (README + Makefile)
                                             ── Phase 2 done (AC #1)
                                                 └─> 12 (pre-flight)
                                                      └─> 13 (gh repo create)
                                                           └─> 14 (set secrets)
                                                                └─> 15 (init + push, local git config)
                                                                     └─> 16 (first run + coexistence + secret-leak check) ── AC #2, #6, #8
                                                                          └─> 17 (cache hit) ── AC #3
                                                                               └─> 18 (smoke) ── AC #4
                                                                                    └─> 19 (heartbeat) ── AC #9
                                                                                         └─> 20 (refresh-key) ── AC #10
                                                                                              └─> 21 (env-missing, LAST) ── AC #5
                                                                                                   ── Phase 3 done
```

## Notes

- **Memory rule applied:** plan goes through dual-review before
  `/plan_w_team` per `dual-review-before-next-phase`.
- **Memory rule applied:** task 15 uses repo-local `git config`, never
  `--global`, per `git-config-never-global`.
- **No follow-up scope creep** beyond spec §10 AC #10.
- **Effort estimate (revised):**
  - Phase 1: ~1.5 hours (config + factory + 3 test-file edits + careful
    fixture updates).
  - Phase 2: ~1 hour (orchestrator hook + 3 YAMLs + script edit +
    README).
  - Phase 3: ~30 min Brian-interactive.
  - **Total: ~3 hours.**

When ready, execute with:
`/cook /Users/briancongelliere/claude/projects/toy-restock-monitor-cloud/specs/plan.md`
(after `/plan_w_team`).

## Changelog

**rev 2 (post plan-review)** — major:
- **Task 1 → 2 split:** task 2 explicitly rewrites `config.example.yaml`
  to v2 shape (was implicit in rev 1, would have broken downstream tests).
- **Task 3 added explicit subtasks** for `redsky_key` (add to
  `_REQUIRED_TOP_KEYS`, dataclass, constructor) — rev 1 omitted this and
  the `_resolve_env` walk would have had nowhere to write.
- **Env var name corrected:** `REDSKY_KEY` → `TARGET_REDSKY_KEY`
  throughout (matches v1's existing `_target_key.py` env-var name; no
  code rename in `_target_key.py` needed).
- **Task 6 rewrite of test_notify_mirror_and_factory.py is now
  explicit** — rev 1 said "keep as-is" but the file has a top-level
  `from src.notify.imessage import ImessageNotifier` that would
  ImportError after task 5.
- **Task 7 explicit heredoc YAML rewrites** in `test_main_bootstrap.py`
  (lines 13–14 and 27–28 of v1).
- **Task 8 TEST_FORCE_IN_STOCK semantic correction:** override path now
  SKIPS the `alerts` table write (no real-cooldown contamination). Spec
  §6.2 was updated in rev 3 to reflect this. Test #4 added to verify
  re-firing within 60 min doesn't cooldown-block.
- **Task 8 placement correction:** override happens AFTER
  `_insert_snapshot` and parse-counter reset, not before, to avoid
  clobbering legitimate counter state.
- **Task 9 actionlint precondition** mandated, not optional.
- **Task 10 script Linux portability:** drop the `.venv/bin/python`
  heredoc; the script must run on a bare Linux Actions runner.
- **Task 13 — drop `--confirm` flag** from `gh repo create` (doesn't
  exist in current CLI).
- **Spec §9.1 cache-delete command corrected** (in spec rev 3):
  `gh cache delete state-v1 --succeed-on-no-caches` (was an invalid
  `gh actions-cache delete state-v1 --confirm`).
- **Task 15 git identity** scoped to local repo, never `--global`.
- **Task 16 honest wait time:** 15–60 min for first fire on new repo;
  resync workaround documented.
- **Task 16 explicit AC #6 grep + AC #8 launchctl/sqlite checks** —
  rev 1 had these as "incidental."
- **Task 21 (env-missing) moved to LAST** so cache verification (AC #3)
  isn't poisoned. One-liner that bundles delete + dispatch + observe +
  restore to minimize the window REDSKY_KEY is missing.

**rev 1** — initial plan.
