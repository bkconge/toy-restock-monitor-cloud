# Plan: Toy Restock Monitor Cloud (v2) — Team Plan

> Team-orchestrated build plan derived from `plan.md` (rev 2, post plan-review).
> Source spec: `toy-restock-monitor-cloud.md` (rev 3, locked).
>
> Task type: **feature (cloud-deployment fork)** · Complexity: **medium-low** ·
> Estimated wall-clock: ~3 hours including manual deploy steps with Brian.

## Task Description

Execute the v2 build per `plan.md` rev 2: fork v1 source, make targeted edits
(drop iMessage, add env: resolver + CloudNtfyNotifier + TEST_FORCE_IN_STOCK
hook), write three GitHub Actions workflows, then create `bkconge/toy-restock-monitor-cloud`,
set secrets, push, and verify all 10 acceptance criteria from spec §10.

Smaller scope than v1 — most src/ is byte-identical-forked. **One builder
agent** handles all of Phase 1 + 2 (the local fork + edit + test + workflow
YAML work). The team lead (main session) handles Phase 3 directly via Bash
+ AskUserQuestion because Phase 3 needs Brian at the keyboard for several
confirmations (first-run wait, smoke-push receipt, possibly Settings → Actions
permission toggle).

## Objective

All 10 acceptance criteria from spec rev 3 §10 PASS:
- Local: full test suite ~55 green; `actionlint .github/workflows/*.yml` clean.
- Deployed: public repo on bkconge ticking via Actions cron; ntfy push with
  `[Cloud] ` prefix arrives on Brian's iPhone via the TEST_FORCE_IN_STOCK
  smoke; v1 still running on Mac unchanged.

## Relevant Files

- `specs/toy-restock-monitor-cloud.md` (rev 3) — locked spec.
- `specs/plan.md` (rev 2) — atomic task list. **Builder reads this before
  working.**
- `/Users/briancongelliere/claude/projects/toy-restock-monitor/src/**` — v1
  source forked verbatim where possible (read-only reference for builder).
- `/Users/briancongelliere/claude/projects/toy-restock-monitor/config.yaml`
  — local-only file; team lead reads `ntfy_topic_url` for Phase 3 task 14.
- `/Users/briancongelliere/claude/projects/toy-restock-monitor/src/sources/_target_key.py`
  — local; team lead reads `_DEFAULT_REDSKY_KEY` for Phase 3 task 14.
- `~/claude/CLAUDE.md` — global engineering rules.
- `~/claude/agents/builder.md`, `~/claude/agents/validator.md` — role
  definitions.
- Memory: `dual-review-before-next-phase`, `git-config-never-global`,
  `ntfy-ios-no-shortcut-trigger`.

## Team Orchestration

- Team lead (me) NEVER writes code directly during Phase 1 + 2 — dispatches
  builder agent.
- Team lead DOES drive Phase 3 directly via Bash + AskUserQuestion, because
  the manual deploy steps need Brian's confirmations mid-flow (waiting for
  first scheduled run, confirming smoke push arrival).
- Validator runs read-only after Phase 1 + 2 (one gate) and at the very end
  (final acceptance).
- `resume: true` on the builder so re-dispatch on validator FAIL preserves
  context.

### Team Members

**builder-local** (agent type: `builder`, model: opus, full tools, `resume: true`)
- Role: execute plan.md tasks 1–11 in order. Single dispatch covering
  the entire local fork + edit + test + workflow YAML work. Builds
  ON TOP of v1 source at the sibling project directory; does not modify v1.
- Plan tasks: **1 through 11** (Phases 1 + 2 of plan rev 2).
- Owns: everything in `/Users/briancongelliere/claude/projects/toy-restock-monitor-cloud/`
  EXCEPT git-init / push / remote-create (those are deploy concerns).

**validator-acceptance** (agent type: `validator`, model: sonnet, read-only, `resume: true`)
- Role: at end of Phase 1+2, verify spec §10 acceptance criteria #1 (workflow
  YAML valid) and #7 (local test suite ≥55 green). At end of Phase 3, verify
  the remaining criteria #2-#6, #8-#10 with command-output evidence.
- Invoked **2 times**: once after `local-build`, once after `deploy-final`.

**team-lead-deploy** (no agent — main session)
- Role: Phase 3 manual steps via direct Bash + AskUserQuestion. Pauses
  for Brian at: confirming email value, confirming smoke push receipt,
  re-trying first scheduled run if it doesn't fire within 60 min.
- Why not a builder agent: subagents don't have clean pause-for-user-input
  mid-task. The team lead's direct execution + AskUserQuestion is the
  right primitive here.

## Step by Step Tasks

Each task maps to a `TaskCreate` call during `/cook`.

### 1. Local build (Phase 1 + 2 — all tasks 1–11 from plan rev 2)
- **Task ID**: `local-build`
- **Depends On**: none
- **Assigned To**: builder-local
- **Agent Type**: builder
- **Parallel**: false (sequential within the dispatch)
- Builder reads `plan.md` rev 2 §"Step by Step Tasks" #1–#11 and executes
  in order. Single agent dispatch, NOT split — the tasks have linear
  dependencies so splitting would only add overhead.
- Owns the HARD CHECKPOINT at task #1 ("if `make test` != 67/67 from v1 fork,
  STOP").
- Reports back with: file list, test count delta, actionlint results,
  any ambiguities resolved, any CI cycle hits (max 2).

### 2. Validate Phase 1 + 2 gate
- **Task ID**: `validate-local`
- **Depends On**: `local-build`
- **Assigned To**: validator-acceptance
- **Agent Type**: validator
- **Parallel**: false
- Verifies spec §10 #1 (workflow YAML valid) and #7 (~55 local tests pass).
- Specifically checks:
  - `make test` exits 0 with ≥55 tests.
  - `actionlint .github/workflows/*.yml` exits 0.
  - `config.example.yaml` is in v2 shape (no `imessage_to`, no
    `per_source_overrides`, `notifier: ntfy`, `parse_fail_threshold: 5`).
  - `src/notify/imessage.py` does NOT exist (deleted per task 5).
  - `src/notify/cloud_ntfy.py` exists with the `[Cloud] ` prefix wrapper.
  - `src/orchestrator.py` contains `TEST_FORCE_IN_STOCK` handling that
    SKIPS the `alerts` table write (spec §6.2 anti-contamination).
  - `scripts/refresh-redsky-key.sh` has NO `.venv/bin/python` reference.
  - `gh cache delete state-v1 --succeed-on-no-caches` appears in the
    workflow (NOT the old invalid `gh actions-cache delete --confirm`).
  - `TARGET_REDSKY_KEY` (not `REDSKY_KEY`) is the env var name everywhere.
- **Gate:** if FAIL, re-dispatch builder-local with `resume: true` and the
  failure list. Do not advance to Phase 3 until PASS.

### 3. Deploy step A — pre-flight (Phase 3 task #12 from plan)
- **Task ID**: `deploy-preflight`
- **Depends On**: `validate-local`
- **Assigned To**: team-lead-deploy
- **Agent Type**: n/a (team lead via Bash)
- **Parallel**: false
- `gh auth status` confirms bkconge.
- Read the local-only values:
  - `NTFY=$(grep ntfy_topic_url ../toy-restock-monitor/config.yaml | sed 's/.*"\(.*\)".*/\1/')`
  - `KEY=$(grep _DEFAULT_REDSKY_KEY ../toy-restock-monitor/src/sources/_target_key.py | sed 's/.*"\(.*\)".*/\1/')`
- Re-confirm local checkpoint (`make test`, `actionlint`).
- **No Brian interaction needed.** Outputs the captured values to the session.

### 4. Deploy step B — create public repo (Phase 3 task #13)
- **Task ID**: `deploy-repo-create`
- **Depends On**: `deploy-preflight`
- **Assigned To**: team-lead-deploy
- **Agent Type**: n/a
- **Parallel**: false
- `gh repo create bkconge/toy-restock-monitor-cloud --public --description
  "..."` (no `--confirm` flag — verified absent from current gh CLI).
- Verify with `gh repo view bkconge/toy-restock-monitor-cloud`.

### 5. Deploy step C — set secrets (Phase 3 task #14)
- **Task ID**: `deploy-set-secrets`
- **Depends On**: `deploy-repo-create`
- **Assigned To**: team-lead-deploy
- **Agent Type**: n/a
- **Parallel**: false
- `gh secret set NTFY_TOPIC_URL --body "$NTFY"` and
  `gh secret set TARGET_REDSKY_KEY --body "$KEY"` on the new repo.
- Verify with `gh secret list`.

### 6. Deploy step D — initial commit + push (Phase 3 task #15)
- **Task ID**: `deploy-push`
- **Depends On**: `deploy-set-secrets`
- **Assigned To**: team-lead-deploy
- **Agent Type**: n/a
- **Parallel**: false
- `git init` + `git config user.name "Brian Congelliere"` and
  `git config user.email "briancongelliere@gmail.com"` LOCAL to the
  repo (NEVER `--global` per memory `git-config-never-global`).
- `git remote add origin
  https://github.com/bkconge/toy-restock-monitor-cloud.git`.
- `git add -A && git status` — review staged file list.
- **Brian touchpoint:** team lead pauses here to show Brian the
  `git status` output and confirm before committing.
- `git commit -m "Initial v2 cloud edition ..."` and
  `git branch -M main && git push -u origin main`.
- Verify on GitHub web UI (or `gh repo view --web` if Brian wants).

### 7. Deploy step E — first scheduled run + coexistence verification (Phase 3 task #16)
- **Task ID**: `deploy-first-run`
- **Depends On**: `deploy-push`
- **Assigned To**: team-lead-deploy
- **Agent Type**: n/a
- **Parallel**: false
- Watch for first scheduled run. **Honest expectation: 15–60 min** for a
  brand-new repo (cron schedule registration delay). If no run appears within
  60 min, push a no-op commit to force resync.
- Use `gh run watch <id>` to follow once a run appears.
- Verify `Print summary` step shows ≥3 snapshot rows.
- **Coexistence check:** run on Brian's Mac:
  ```bash
  cd ~/claude/projects/toy-restock-monitor
  sqlite3 data/state.db "SELECT MAX(fetched_at) FROM snapshots;"
  launchctl list | grep toy-restock-monitor
  ```
  Expect v1's last snapshot < 10 min ago and launchctl entry present.
- **Secret-leak check:** `gh run view <id> --log | grep -F "$TOPIC_SUFFIX"` —
  expect no match.
- **Brian touchpoint:** if waiting for first fire takes > 15 min, AskUserQuestion
  whether to push the resync commit or continue waiting.

### 8. Deploy step F — second run cache hit (Phase 3 task #17)
- **Task ID**: `deploy-cache-hit`
- **Depends On**: `deploy-first-run`
- **Assigned To**: team-lead-deploy
- **Agent Type**: n/a
- **Parallel**: false
- Wait for second `:NN` 5-min slot (or trigger via `gh workflow run`).
- `gh run view <id> --log` — search for `Restore state cache` output;
  confirm `cache-hit: true`.

### 9. Deploy step G — smoke test (Phase 3 task #18)
- **Task ID**: `deploy-smoke-test`
- **Depends On**: `deploy-cache-hit`
- **Assigned To**: team-lead-deploy
- **Agent Type**: n/a
- **Parallel**: false
- `gh workflow run restock-monitor.yml -f force_in_stock=squeeezy-cheese`.
- `gh run watch <id>`.
- **Brian touchpoint:** AskUserQuestion "did the `[Cloud]` ntfy push arrive
  on your phone?" Only mark PASS when Brian confirms.
- Anti-contamination sanity: `gh run view <id> --log | grep "alerts row NOT
  written"` — confirm the bypass actually fired.

### 10. Deploy step H — heartbeat workflow (Phase 3 task #19)
- **Task ID**: `deploy-heartbeat`
- **Depends On**: `deploy-smoke-test`
- **Assigned To**: team-lead-deploy
- **Agent Type**: n/a
- **Parallel**: false
- `gh workflow run heartbeat.yml`.
- `gh run watch <id>`.
- Verify a new commit on `main`:
  `gh api repos/bkconge/toy-restock-monitor-cloud/commits | jq '.[0].commit.message'`
  shows `heartbeat: ...`.
- **If 403 on push step:** AskUserQuestion to guide Brian through Settings →
  Actions → General → Workflow permissions → Read and write permissions.
  Then re-run.

### 11. Deploy step I — refresh-redsky-key workflow (Phase 3 task #20)
- **Task ID**: `deploy-refresh-key`
- **Depends On**: `deploy-heartbeat`
- **Assigned To**: team-lead-deploy
- **Agent Type**: n/a
- **Parallel**: false
- `gh workflow run refresh-redsky-key.yml`.
- `gh run view <id> --log | grep -E "^[0-9a-f]{40}$"` — expect a 40-hex
  string in the log.
- Brian does NOT need to rotate the secret here.

### 12. Deploy step J — env-missing-fails-fast (Phase 3 task #21 — LAST)
- **Task ID**: `deploy-env-missing-test`
- **Depends On**: `deploy-refresh-key`
- **Assigned To**: team-lead-deploy
- **Agent Type**: n/a
- **Parallel**: false
- **Last in Phase 3 deliberately** — deleting `TARGET_REDSKY_KEY` could
  break a scheduled run if anything else depended on a populated cache.
  By running last, no later task is at risk.
- Use the bundled one-liner from plan.md task #21 (delete → dispatch →
  observe → restore in sequence; restoration target < 60s).

### 13. Final validation
- **Task ID**: `validate-final`
- **Depends On**: `deploy-env-missing-test`
- **Assigned To**: validator-acceptance
- **Agent Type**: validator
- **Parallel**: false
- Verifies spec §10 #2, #3, #4, #5, #6, #8, #9, #10.
- Reports PASS/FAIL per criterion with command-output evidence.
- v2 ship status: GREEN only when all 10 criteria PASS (criteria #1 and
  #7 were already PASSed at `validate-local` gate).

## Acceptance Criteria

Mirrors spec rev 3 §10 #1–#10. Plan completion = all 10 PASS at
`validate-final`.

| # | Criterion (abbrev)                  | Validated at |
|---|-------------------------------------|--------------|
| 1 | Workflow YAML valid (actionlint)    | validate-local |
| 2 | First scheduled run within 30 min, ≥3 snapshots | deploy-first-run + validate-final |
| 3 | Second run reports cache-hit: true  | deploy-cache-hit + validate-final |
| 4 | TEST_FORCE_IN_STOCK delivers `[Cloud]` push | deploy-smoke-test + validate-final |
| 5 | Env-var-missing fails fast          | deploy-env-missing-test + validate-final |
| 6 | No secrets in workflow logs         | deploy-first-run + validate-final |
| 7 | Local test suite ≥55 green          | validate-local |
| 8 | Coexistence — v1 still ticks, same topic | deploy-first-run + validate-final |
| 9 | Heartbeat workflow ran in last 7 days | deploy-heartbeat + validate-final |
| 10 | Refresh-key workflow prints parseable key | deploy-refresh-key + validate-final |

## Validation Commands

Per plan.md §"Validation Commands":

```bash
# Phase 1 + 2 gate (validate-local):
make test                                                              # ~55 green
make lint-workflows                                                    # actionlint clean
test -f src/notify/cloud_ntfy.py && ! test -f src/notify/imessage.py   # right files
grep -F "TARGET_REDSKY_KEY" .github/workflows/restock-monitor.yml      # right env var
grep -F "gh cache delete state-v1 --succeed-on-no-caches" .github/workflows/restock-monitor.yml  # right cache cmd
grep -F "alerts row NOT written" src/orchestrator.py                   # cooldown bypass present

# Phase 3 gate (validate-final):
gh run list --workflow restock-monitor.yml --limit 5                   # multiple successes
gh run list --workflow heartbeat.yml --limit 1                         # one success
gh run list --workflow refresh-redsky-key.yml --limit 1                # one success
gh secret list --repo bkconge/toy-restock-monitor-cloud                # both secrets restored (post env-missing test)
sqlite3 ../toy-restock-monitor/data/state.db "SELECT MAX(fetched_at) FROM snapshots;"  # v1 still ticking
# Brian confirms [Cloud] push delivered (deploy-smoke-test)
```

## Dependency Graph

```
local-build (1)  ── builder-local single dispatch covering all of Phase 1+2
 └─> validate-local (2)  ── validator-acceptance: AC #1, #7
      └─> deploy-preflight (3)  ──┐
           └─> deploy-repo-create (4)  │
                └─> deploy-set-secrets (5)  │ ALL team-lead-deploy
                     └─> deploy-push (6) ── │ (no agent dispatch;
                          └─> deploy-first-run (7)  │  direct Bash + AskUserQuestion)
                               └─> deploy-cache-hit (8)
                                    └─> deploy-smoke-test (9)
                                         └─> deploy-heartbeat (10)
                                              └─> deploy-refresh-key (11)
                                                   └─> deploy-env-missing-test (12)  ── LAST
                                                        └─> validate-final (13)  ── validator-acceptance: AC #2-6, #8-10
                                                             ── DONE
```

No parallel slots. Single-builder + single-validator pattern is right-sized
for v2's small change surface.

## Notes

- **Per Brian's memory rule** `dual-review-before-next-phase`: both the spec
  (rev 1 → 2 → 3) and plan (rev 1 → 2) have already been through dual
  review and revisions. This team plan does not re-review — it converts
  the reviewed plan into task assignments. No further review gate before
  /cook.
- **Per Brian's memory rule** `git-config-never-global`: task `deploy-push`
  uses `git config user.name`/`user.email` LOCAL to the repo, never
  `--global`. The builder-local agent does NOT touch git at all.
- **Per Brian's memory rule** `ntfy-ios-no-shortcut-trigger`: v2 sends
  ntfy only. No iOS Shortcut bridge attempted. v1's iMessage stays the
  Mac-awake channel.
- **`resume: true`** on builder-local preserves context if validate-local
  FAILs and rework is needed.
- **Validator-acceptance also `resume: true`** so the final validation
  doesn't restart from cold context.
- **No follow-up scope beyond spec §10 AC #10.** v3 items (curl_cffi for
  Walmart/Schylling, automated key rotation) stay out per spec §11.

When ready, execute the team plan with:
`/cook /Users/briancongelliere/claude/projects/toy-restock-monitor-cloud/specs/plan-team.md`
