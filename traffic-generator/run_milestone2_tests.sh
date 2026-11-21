#!/usr/bin/env bash
# Milestone 2 acceptance tests: weighted routing + runtime weight changes.
#
# Usage (from the load-balancer/ directory, with `docker compose up -d` done):
#   ./traffic-generator/run_milestone2_tests.sh            # 10000 req/test
#   REQUESTS=2000 ./traffic-generator/run_milestone2_tests.sh
#
# Leaves HAProxy at 50:50 (the boot default) when finished.

set -u
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
REQUESTS="${REQUESTS:-10000}"
TOL="${TOLERANCE:-2}"
SET="$PY runtime/set_weights.py"
TEST="$PY traffic-generator/test_distribution.py --requests $REQUESTS --tolerance $TOL"

PASSED=0
FAILED=0
pass() { echo "  -> RESULT: PASS ($1)"; PASSED=$((PASSED + 1)); }
fail() { echo "  -> RESULT: FAIL ($1)"; FAILED=$((FAILED + 1)); }
banner() { echo; echo "=================================================================="; echo "$1"; echo "=================================================================="; }

started_at() { docker inspect -f '{{.State.StartedAt}}' haproxy; }
haproxy_pid() { echo "show info" | docker exec -i haproxy socat stdio unix-connect:/var/lib/haproxy/admin.sock | awk -F': ' '/^Pid:/{print $2}'; }

banner "Pre-check: Milestone 1 endpoints"
for url in http://localhost:8080/ http://localhost:8001/ http://localhost:8002/; do
  if out=$(curl -fsS --max-time 5 "$url"); then
    echo "  $url -> $(echo "$out" | $PY -c 'import json,sys; print(json.load(sys.stdin)["backend"])')"
  else
    echo "  $url -> UNREACHABLE"; fail "endpoint $url"
  fi
done

banner "Unit tests: validation, ordering, rollback (offline)"
if $PY runtime/test_haproxy_runtime.py; then pass "unit tests"; else fail "unit tests"; fi

run_static() {  # name a b
  banner "$1: $2:$3 distribution"
  if $TEST --a "$2" --b "$3" --apply; then pass "$2:$3"; else fail "$2:$3"; fi
}

run_static "Test 1" 90 10
run_static "Test 2" 80 20
run_static "Test 2b" 75 25
run_static "Test 3" 50 50
run_static "Test 4" 100 0
run_static "Test 5" 0 100

banner "Test 6: Dynamic configuration (90:10 -> 80:20 -> 50:50, no restart)"
T0=$(started_at); P0=$(haproxy_pid)
echo "  HAProxy container StartedAt=$T0  worker Pid=$P0"
ok=1
for w in "90 10" "80 20" "50 50"; do
  set -- $w
  echo; echo "  \$ python runtime/set_weights.py --a $1 --b $2"
  $SET --a "$1" --b "$2" || ok=0
  $TEST --a "$1" --b "$2" || ok=0
done
T1=$(started_at); P1=$(haproxy_pid)
echo "  HAProxy container StartedAt=$T1  worker Pid=$P1"
if [ "$T0" = "$T1" ] && [ "$P0" = "$P1" ] && [ $ok -eq 1 ]; then
  pass "weights changed live; container and process unchanged"
else
  fail "dynamic reconfiguration (ok=$ok, StartedAt $T0 vs $T1, Pid $P0 vs $P1)"
fi

banner "Test 7: Invalid configurations are rejected and weights preserved"
$SET --a 80 --b 20 >/dev/null
BEFORE=$($SET --show | awk '/weight=/{print $2}' | tr '\n' ' ')
echo "  weights before: $BEFORE"
expect_reject() {  # description, args...
  local desc="$1"; shift
  echo; echo "  [$desc] \$ python runtime/set_weights.py $*"
  $SET "$@"; rc=$?
  if [ $rc -ne 0 ]; then echo "  rejected (exit $rc)"; else echo "  ACCEPTED (unexpected)"; ok7=0; fi
}
ok7=1
expect_reject "negative A"          --a -10 --b 20
expect_reject "negative B"          --a 80 --b -1
expect_reject "both zero"           --a 0 --b 0
expect_reject "non-integer"         --a abc --b 20
expect_reject "decimal"             --a 1.5 --b 20
expect_reject "above HAProxy max"   --a 300 --b 20
expect_reject "missing --b"         --a 80
expect_reject "missing --a"         --b 20
expect_reject "no arguments"
expect_reject "unknown server"      --a 50 --b 50 --server-b backend-x
echo; echo "  [test script] \$ python traffic-generator/test_distribution.py --a 0 --b 0"
$PY traffic-generator/test_distribution.py --a 0 --b 0 --requests 10 && ok7=0
AFTER=$($SET --show | awk '/weight=/{print $2}' | tr '\n' ' ')
echo; echo "  weights after:  $AFTER"
if [ $ok7 -eq 1 ] && [ "$BEFORE" = "$AFTER" ]; then pass "all invalid input rejected, config preserved"; else fail "invalid input handling"; fi

banner "Cleanup: restore boot default 50:50"
$SET --scenario equal

banner "SUMMARY: $PASSED passed, $FAILED failed"
[ $FAILED -eq 0 ]
