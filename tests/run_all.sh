#!/usr/bin/env bash
# Run all phase smoke tests in order. Continues past failures; aggregates.
# Usage: ./run_all.sh            # all phases
#        PHASES="1 3" ./run_all.sh   # subset
set -u
cd "$(dirname "$0")"
: "${PHASES:=1 2 3 4 5}"
declare -A FILES=(
  [1]=smoke_phase1_controller.sh
  [2]=smoke_phase2_dra.sh
  [3]=smoke_phase3_dataplane.sh
  [4]=smoke_phase4_health.sh
  [5]=smoke_phase5_scheduler.sh
)
rc=0
for p in $PHASES; do
  f="${FILES[$p]:-}"
  [ -n "$f" ] || { echo "unknown phase $p"; continue; }
  bash "./$f" || rc=1
done
echo
if [ "$rc" -eq 0 ]; then echo "ALL PHASES: no hard failures (SKIP = not implemented yet)"
else echo "SOME PHASES HAD FAILURES (see [FAIL] above)"; fi
exit "$rc"
