#!/usr/bin/env bash
# refresh-redsky-key.sh — extract the current public RedSky API key from
# Target's homepage HTML/JS and print it to stdout.
#
# v2 (cloud edition): no `.venv` dependency and no in-place file rewrite.
# Designed to run on a bare GitHub Actions ubuntu-latest runner. The key is
# printed to stdout for Brian to copy into the TARGET_REDSKY_KEY GitHub
# Secret via `gh secret set` from his Mac (manual rotation — see spec R3).
#
# Strategy (best-effort, may need tuning if Target's bundle layout shifts):
#   1. Fetch https://www.target.com/ with a realistic UA.
#   2. From the HTML, extract any inline 40-hex token sitting next to
#      'apiKey' / 'key:' references (high precision), with a global
#      most-common 40-hex fallback (lower precision).

set -uo pipefail

UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

tmp_html="$(mktemp)"
trap 'rm -f "$tmp_html"' EXIT

if ! curl -sSL --max-time 20 \
    -H "User-Agent: $UA" \
    -H "Accept-Language: en-US,en;q=0.9" \
    -H "Accept-Encoding: gzip" \
    --compressed \
    "https://www.target.com/" -o "$tmp_html"; then
    echo "ERROR: could not fetch https://www.target.com/ homepage" >&2
    exit 1
fi

if [[ ! -s "$tmp_html" ]]; then
    echo "ERROR: empty homepage response" >&2
    exit 1
fi

key_from_marker="$(
    grep -oE '"(apiKey|key)"[^"]{0,5}"[a-f0-9]{40}"' "$tmp_html" 2>/dev/null \
        | grep -oE '[a-f0-9]{40}' \
        | sort | uniq -c | sort -rn | head -n1 \
        | awk '{print $2}'
)"

if [[ -z "${key_from_marker:-}" ]]; then
    key_from_marker="$(
        grep -oE '[a-f0-9]{40}' "$tmp_html" 2>/dev/null \
            | sort | uniq -c | sort -rn | head -n1 \
            | awk '{print $2}'
    )"
fi

if [[ -z "${key_from_marker:-}" ]]; then
    echo "ERROR: no 40-hex token found in Target homepage HTML." >&2
    echo "Target's bundle layout may have changed." >&2
    exit 2
fi

NEW_KEY="$key_from_marker"

if [[ ! "$NEW_KEY" =~ ^[a-f0-9]{40}$ ]]; then
    echo "ERROR: extracted key '$NEW_KEY' is not a 40-hex string." >&2
    exit 3
fi

echo "$NEW_KEY"
echo >&2
echo "Copy the key above and run from your Mac:" >&2
echo "  gh secret set TARGET_REDSKY_KEY --body \"$NEW_KEY\" --repo bkconge/toy-restock-monitor-cloud" >&2
