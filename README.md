# toy-restock-monitor-cloud (v2)

24/7 cloud companion to [`toy-restock-monitor`](https://github.com/bkconge/toy-restock-monitor) (v1).
v1 runs on Brian's Mac via launchd when the laptop is awake; v2 runs on
GitHub Actions cron so coverage continues overnight and during travel.
Both publish to the **same ntfy topic** so alerts arrive on the same
phone subscription. v2's body is prefixed with `[Cloud] ` so the source
is obvious at a glance.

See `specs/toy-restock-monitor-cloud.md` for the locked spec (rev 3).

## Setup precondition

```bash
brew install actionlint   # for `make lint-workflows`
gh auth status            # should show bkconge
```

## Quickstart (fresh clone)

```bash
git clone https://github.com/bkconge/toy-restock-monitor-cloud
cd toy-restock-monitor-cloud
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
make test
actionlint .github/workflows/*.yml
```

## Deploy

One-time, from a Mac with `gh` logged in as `bkconge`:

```bash
gh repo create bkconge/toy-restock-monitor-cloud --public \
  --description "24/7 cloud companion to toy-restock-monitor; ntfy alerts via GitHub Actions"

# Pull values from v1's local config — never type them in plaintext:
NTFY=$(grep ntfy_topic_url ../toy-restock-monitor/config.yaml | sed 's/.*"\(.*\)".*/\1/')
KEY=$(grep _DEFAULT_REDSKY_KEY ../toy-restock-monitor/src/sources/_target_key.py | sed 's/.*"\(.*\)".*/\1/')
gh secret set NTFY_TOPIC_URL    --body "$NTFY" --repo bkconge/toy-restock-monitor-cloud
gh secret set TARGET_REDSKY_KEY --body "$KEY"  --repo bkconge/toy-restock-monitor-cloud

git init
git config user.name  "Brian Congelliere"
git config user.email "<your-bkconge-email>"
git remote add origin https://github.com/bkconge/toy-restock-monitor-cloud.git
git add -A
git commit -m "Initial v2 cloud edition"
git branch -M main
git push -u origin main
```

## How alerts work

- `restock-monitor.yml` runs every 5 minutes on `cron: '7-59/5 * * * *'`.
  Effective cadence is 10–20 min in practice (GitHub Actions cron drift).
- When a watched product is in stock at or under its MSRP cap, v2 POSTs
  to ntfy. v1 (if your Mac is awake) also sends iMessage. v2's ntfy
  payload is prefixed `[Cloud] ` so you can tell the source apart.
- Cooldown is 60 minutes per `(watch_id, source_url, sku)` — v1 and v2
  cooldowns are independent (separate databases), so a real restock may
  ping you up to 4 times in 30s during the comparison window. Tolerable.

## Smoke test (verify ntfy delivery from Actions)

```bash
gh workflow run restock-monitor.yml -f force_in_stock=squeeezy-cheese
```

Within ~1 minute of the run starting, expect a `[Cloud] 🟢 IN STOCK at
MSRP — Squeeezy Cheese …` push. The override **skips the `alerts` table
write** so the smoke does NOT poison the 60-min cooldown on a real
restock of the same `watch_id` — spec §6.2.

## Operations

```bash
gh run list --workflow restock-monitor.yml --limit 5   # recent ticks
gh run view <run-id> --log                             # full log of one run
make test                                              # local unit tests
make lint-workflows                                    # actionlint workflows
```

## Refresh the Target RedSky key

When alerts stop arriving and you suspect Target rotated their public
API key (parser_broken alerts firing):

```bash
gh workflow run refresh-redsky-key.yml
# Wait ~30s, then:
gh run view <run-id> --log | grep -E "^[0-9a-f]{40}$"
# Copy that key, then from your Mac:
gh secret set TARGET_REDSKY_KEY --body "<key>" --repo bkconge/toy-restock-monitor-cloud
```

Manual rotation is intentional (spec R3 / §7) — no PAT for secret-write
keeps blast radius small.

## Troubleshooting

- **Workflow auto-disabled.** GitHub disables scheduled workflows on
  public repos after 60 days of repo inactivity. The `heartbeat`
  workflow commits a `last-heartbeat.txt` line every Monday to prevent
  this. If it ever happens anyway, re-enable in the Actions UI.
- **Persistent 403s from Target.** Datacenter IPs face more aggressive
  Akamai challenges than residential. The default
  `parse_fail_threshold: 5` was raised from v1's 3 to dampen noise. If
  failure rate exceeds 20% sustained, residential-proxy is the v3 scope.
- **No pushes arriving.** Verify `NTFY_TOPIC_URL` secret matches your
  ntfy app subscription, and that the heartbeat workflow's most recent
  push succeeded (it needs Settings → Actions → General → Workflow
  permissions → "Read and write permissions").

## Coexistence with v1

`[Cloud] ` prefix distinguishes v2's pushes. Up to four notifications
per restock within 30s (1 ntfy + 1 iMessage per system × 2 systems) is
the worst case during the 1–2 week comparison window. If it becomes
annoying, retire whichever side you trust less.
