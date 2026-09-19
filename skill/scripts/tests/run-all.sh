#!/usr/bin/env bash
# Run every self-check this skill has. No running stack required -- these test the SCRIPTS,
# not a migration, so they work on a clean clone.
#
#   ./scripts/tests/run-all.sh
#
# Exits non-zero if any fails, so it can gate a change to the skill.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0
run() {
  printf '\n\033[1m%s\033[0m\n' "$1"; shift
  "$@" || fail=$((fail + 1))
}

# The panel parser, against a fixture exercising every panel shape it claims to handle.
# Output is a table a human reads, so this asserts only that it parses without error -- read
# the table when you change the parser.
run "inventory-panels.py — parses every panel shape in the fixture" \
    bash -c 'python3 tests/make-fixture.py > /tmp/skill-fixture.ndjson &&
             python3 inventory-panels.py /tmp/skill-fixture.ndjson > /dev/null &&
             echo "  ok   parsed the fixture with no error"'

# plan-collector's output is a classification, where a wrong answer looks like a right one.
run "plan-collector.py — classifies every verdict correctly" \
    python3 tests/test-plan-collector.py

# The loader's schema decisions. A wrong column TYPE survives every later check: the load
# succeeds, the tiles render, and a metric is quietly truncated or a NULL is quietly a zero.
run "ecs-to-clickhouse.py — types every ECS field the way the target needs it" \
    python3 tests/test-ecs-loader.py

# The two halves of the receiver mapping are written twice on purpose; nothing else notices
# when they diverge.
run "receiver-map.json vs integration-to-receiver.md — still agree" \
    python3 tests/check-map-doc-sync.py

printf '\n'
if [ "$fail" -eq 0 ]; then
  printf '\033[32mAll suites passed.\033[0m\n'; exit 0
fi
printf '\033[31m%s suite(s) failed.\033[0m\n' "$fail"; exit 1
