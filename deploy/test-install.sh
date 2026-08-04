#!/usr/bin/env bash
# Drive install.sh's re-run decisions without running install.sh: the
# functions are lifted out of it verbatim, so what is exercised is the code
# that ships, not a restatement of it.
set -u
SRC=${1:-"$(cd "$(dirname "$0")" && pwd)/install.sh"}

# The bits install.sh provides around them.
info() { printf '[snaplicator] %s\n' "$*" >&2; }
die()  { printf '[snaplicator] ERROR: %s\n' "$*" >&2; exit 1; }

# consider_existing_install, verbatim.
eval "$(awk '/^consider_existing_install\(\) \{/,/^\}/' "$SRC")"

pass=0; fail=0
check() { # desc, expected, actual
  if [ "$2" = "$3" ]; then pass=$((pass+1)); printf '  ok   %s\n' "$1"
  else fail=$((fail+1)); printf '  FAIL %s\n    expected [%s]\n    got      [%s]\n' "$1" "$2" "$3"; fi
}

echo "### nothing installed -> the question is still a question"
CONNSTR=""; DEMO=0; REUSE_EXISTING=0
consider_existing_install "" 2>/dev/null
check "no probe result leaves REUSE_EXISTING alone" 0 "$REUSE_EXISTING"

echo
echo "### installed, no terminal -> re-running means 'open what is here'"
CONNSTR=""; DEMO=0; REUSE_EXISTING=0
consider_existing_install "old.example.com|5432|prod|u|running" < /dev/null 2>/dev/null
check "reuses instead of asking" 1 "$REUSE_EXISTING"

echo
echo "### installed, but a URI was given on the command line"
CONNSTR="postgres://u:p@new.example.com:5432/other"; DEMO=0; REUSE_EXISTING=0
consider_existing_install "old.example.com|5432|prod|u|running" < /dev/null 2>/dev/null
check "an answer already given is not overridden" 0 "$REUSE_EXISTING"

echo
echo "### installed, --demo asked for"
CONNSTR=""; DEMO=1; REUSE_EXISTING=0
consider_existing_install "old.example.com|5432|prod|u|running" < /dev/null 2>/dev/null
check "demo is a decision too" 0 "$REUSE_EXISTING"

echo
echo "### what it prints"
CONNSTR=""; DEMO=0; REUSE_EXISTING=0
out=$(consider_existing_install "old.example.com|5432|prod|u|running" < /dev/null 2>&1)
case "$out" in *"already installed"*) r=yes ;; *) r=no ;; esac
check "says so up front" yes "$r"
case "$out" in *"old.example.com:5432/prod"*) r=yes ;; *) r=no ;; esac
check "names the primary it is already on" yes "$r"
case "$out" in *"running"*) r=yes ;; *) r=no ;; esac
check "and the replica's state" yes "$r"

echo
echo "### the repoint guard's field extraction"
PREV="old.example.com|5432|prod|u|running"
check "state"  running "${PREV##*|}"
check "target" "old.example.com|5432|prod" "${PREV%|*|*}"
PREV="h|5432|d|u|absent"
check "a removed replica is not a conflict" absent "${PREV##*|}"

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
