#!/usr/bin/env bash
# Compare your .env with .env.example (.env.agent.example on an agent) — run it
# after every `git pull`.
#
#   ./env-check.sh            list what's new or unknown
#   ./env-check.sh --append   also add the new settings to .env, commented out
#   ./env-check.sh FILE       check another env file instead of ./.env
#
# Lists settings in .env.example that your .env doesn't mention (with their
# default and explanation) and settings in your .env that .env.example doesn't
# know (usually a typo, or a setting that was renamed). Only setting NAMES are
# read from your .env — its values, and so your keys, are never printed.
#
# A setting you don't have is not an error: ColdVault uses the default shown.
# --append adds them to the end of .env commented out ("# NAME=default"), so
# nothing changes until you uncomment one; a commented-out setting counts as
# seen, so the next check is clean. Apply .env edits with: docker compose up -d
#
# Exit status: 0 = nothing new or unknown, 1 = something listed, 2 = error.

set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
example="$here/.env.example"
env_file=""
append=0

for arg in "$@"; do
  case "$arg" in
    -a|--append) append=1 ;;
    -h|--help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
    -*) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    *) env_file=$arg ;;
  esac
done
env_file=${env_file:-$here/.env}

[ -f "$env_file" ] || { echo "no $env_file — start with: cp .env.example .env" >&2; exit 2; }
# An agent (second machine) has its own, much shorter list of settings.
if grep -qE '^COLDVAULT_ROLE=agent' "$env_file" && [ -f "$here/.env.agent.example" ]; then
  example="$here/.env.agent.example"
fi
[ -f "$example" ] || { echo "no $(basename "$example") at $example" >&2; exit 2; }
ex=$(basename "$example")

# NAME=... lines, as a sorted list of names.
names() { { grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$1" || true; } | tr -d '=' | sort -u; }
# Names in the env file, counting commented-out "# NAME=..." lines as seen.
seen() {
  { grep -oE '^[ 	]*#?[ 	]*[A-Za-z_][A-Za-z0-9_]*=' "$1" || true; } \
    | sed -E 's/^[ 	]*#?[ 	]*//; s/=$//' | sort -u
}

missing=$(comm -13 <(seen "$env_file") <(names "$example"))
unknown=$(comm -23 <(names "$env_file") <(names "$example"))

# Print each wanted setting from .env.example with the comment block right
# above it, grouped under its "# --- Section ---" header. $2 prefixes the
# NAME=value line ("# " to write it commented out).
describe() {
  awk -v keys="$1" -v pfx="$2" '
    BEGIN { n = split(keys, a, " "); for (i = 1; i <= n; i++) want[a[i]] = 1 }
    /^# ---/            { section = $0; buf = ""; next }
    /^#/                { buf = buf $0 "\n"; next }
    /^[ \t]*$/          { buf = ""; next }
    /^[A-Za-z_][A-Za-z0-9_]*=/ {
      k = $0; sub(/=.*/, "", k)
      if (k in want) {
        if (section != shown) { printf "%s%s\n", (shown == "" ? "" : "\n"), section; shown = section }
        printf "%s%s%s\n", buf, pfx, $0
      }
      buf = ""
    }' "$example"
}

status=0
rel_env=${env_file#"$here"/}

if [ -n "$missing" ]; then
  status=1
  count=$(echo "$missing" | wc -l | tr -d ' ')
  echo "== $count setting(s) in $ex that $rel_env doesn't have =="
  echo "   (ColdVault uses the default shown; add one only to change it)"
  echo
  describe "$(echo $missing)" ""
  echo
fi

if [ -n "$unknown" ]; then
  status=1
  echo "== in $rel_env but not in $ex — a typo, renamed, or your own =="
  echo "$unknown" | sed 's/^/   /'
  echo
fi

if [ "$status" -eq 0 ]; then
  echo "✔ $rel_env mentions every setting in $ex, and nothing unknown."
  exit 0
fi

if [ "$append" -eq 1 ] && [ -n "$missing" ]; then
  # Make sure the block starts on its own line.
  [ -z "$(tail -c1 "$env_file")" ] || echo >> "$env_file"
  {
    echo
    echo "# ===== added by env-check.sh on $(date +%Y-%m-%d): new in $ex ====="
    echo "# Commented out, so ColdVault keeps using the default shown. Uncomment and"
    echo "# edit any you want to change, then: docker compose up -d"
    describe "$(echo $missing)" "# "
  } >> "$env_file"
  echo "✔ appended $count setting(s) to $rel_env, commented out — nothing changes until you uncomment one."
elif [ -n "$missing" ]; then
  echo "Run ./env-check.sh --append to add them to $rel_env, commented out."
fi
exit "$status"
