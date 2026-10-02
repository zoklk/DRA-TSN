#!/usr/bin/env bash
# fig_mixed scenario loop — the live demo driver (ADR-0039 §개정).
#
# This is the reproducible artifact. Not a collector whose output gets replayed:
# the scene shows the cluster as it is, and this script drives the cluster
# through the same action sequence every time.
#
#   repeat N:
#     find the node hosting vplc-line
#     kill it                    (POST /inject)
#     hold T_FAULT               (recovery happens inside; its duration is measured)
#     revive it                  (POST /restore — also pins the node MAC back, so
#                                 the CNC x TSNNic join re-forms and no stream debt
#                                 accumulates across cycles)
#     rebuild the initial state  (run.sh clean + fill)
#     hold T_NORMAL
#
# WHAT IS REPRODUCIBLE IS THE PROCEDURE, NOT THE OUTCOME. The vanilla arm lands
# on a different node from cycle to cycle, and that variance is the result — it
# is what a viewer is meant to see. Averaging it into a baked animation would
# destroy the only thing worth showing.
#
# The loop drives live_serve's HTTP API rather than docker directly, for three
# reasons: one code path writes the fault state, /restore does the MAC pin-back
# for free, and two arms can be injected simultaneously.
#
# Usage:
#   # 1. one server, one poller per arm, timeline logging on
#   python3 live_serve.py --cluster dratsn=~/.kube/kind-dratsn.kubeconfig \
#                         --cluster vanilla=~/.kube/kind-vanilla.kubeconfig \
#                         --state-dir results/live --timeline auto &
#   # 2. drive it
#   CYCLES=10 ./scenario_loop.sh
set -u
cd "$(dirname "$0")"
. ../lib.sh
PHASE="fig_mixed-loop"

: "${CYCLES:=10}"
: "${T_NORMAL:=60}"          # seconds of healthy running before each fault
: "${T_FAULT:=60}"           # seconds held in the faulted state
: "${SERVER:=http://127.0.0.1:8787}"
: "${LOOP_ARMS:=dratsn vanilla}"
: "${RUN:=live}"
# kubeconfig per arm, same pinning rule as everywhere else: never the shared
# current-context, which `kind create cluster` rewrites as a side effect
: "${KUBECONFIG_DRATSN:=$HOME/.kube/kind-dratsn.kubeconfig}"
: "${KUBECONFIG_VANILLA:=$HOME/.kube/kind-vanilla.kubeconfig}"

kubeconfig_of() { case "$1" in dratsn) echo "$KUBECONFIG_DRATSN" ;; *) echo "$KUBECONFIG_VANILLA" ;; esac; }

api() { # $1=method $2=path [$3=json]
  local m="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$m" -H 'Content-Type: application/json' -d "$body" "$SERVER$path"
  else
    curl -sS -X "$m" "$SERVER$path"
  fi
}

# run.sh against one arm's cluster. Its own preflight refuses a non-kind context.
arm_run() { # $1=arm, rest = run.sh args
  local arm="$1"; shift
  KUBECONFIG="$(kubeconfig_of "$arm")" RUN="$RUN" ./run.sh "$@"
}

hdr "scenario loop: $CYCLES cycles x [$LOOP_ARMS], normal=${T_NORMAL}s fault=${T_FAULT}s"
require curl || { summary; exit 1; }
api GET /state >/dev/null 2>&1 || die "live_serve not reachable at $SERVER — start it first (see the header of this file)"

# One-time fixtures per arm. Cheap to repeat and it heals a half-torn-down state.
for arm in $LOOP_ARMS; do
  info "setup($arm)"
  arm_run "$arm" setup >/dev/null || die "setup failed for $arm"
done

restore_all_arms() {
  for arm in $LOOP_ARMS; do
    api POST /restore "{\"cluster\":\"$arm\",\"node\":\"all\"}" >/dev/null 2>&1 || true
  done
}
trap restore_all_arms EXIT

# Cycle windows. plot.py needs to know where each episode starts and ends so it
# can cut out the rebuild between cycles — that gap is an experiment reset, not
# an event, and drawing it would put a spurious dip in the waveform.
CYCLES_CSV="results/$RUN/cycles.csv"
mkdir -p "results/$RUN"
[ -f "$CYCLES_CSV" ] || echo "cycle,t_ready_ms,t_inject_ms,t_restore_ms" > "$CYCLES_CSV"

for c in $(seq 1 "$CYCLES"); do
  hdr "cycle $c/$CYCLES"

  # --- rebuild the identical starting state -------------------------------
  # Full rebuild rather than putting the moved pods back: reproducibility is the
  # point of this script, and an incremental repair accumulates drift. The
  # rebuild is visible in the scene; that is fine, the figure excises it (it is
  # an experiment reset, not an event).
  for arm in $LOOP_ARMS; do
    arm_run "$arm" clean >/dev/null 2>&1
    arm_run "$arm" fill "$arm" >/dev/null || die "fill failed for $arm in cycle $c"
  done
  t_ready=$(now_ms)
  info "both arms filled — holding ${T_NORMAL}s of normal operation"
  sleep "$T_NORMAL"

  # --- fault ---------------------------------------------------------------
  # No node argument: live_serve defaults to the node hosting the vPLC, which
  # the fill has just pinned. Both arms are injected back to back so their
  # timelines line up on screen.
  # Stamped BEFORE the calls: each /inject blocks on its own docker stop, so
  # stamping afterwards put t_inject ~20 s past the actual fault and made every
  # recovery figure computed from it nonsense.
  t_inject=$(now_ms)
  # IN PARALLEL. Injecting the arms one after another put ~10 s between their
  # faults, which shifted one arm's whole event left on a shared time axis and
  # made the slower arm look faster than the other. The arms must fail at the
  # same instant for their curves to be comparable — and for the two zones of
  # the live scene to move together.
  inj_out="$(mktemp -d)"
  for arm in $LOOP_ARMS; do
    ( api POST /inject "{\"cluster\":\"$arm\"}" > "$inj_out/$arm" 2>&1 ) &
  done
  wait
  for arm in $LOOP_ARMS; do
    info "inject($arm): $(head -c 200 "$inj_out/$arm")"
  done
  rm -rf "$inj_out"
  info "holding ${T_FAULT}s in the faulted state"
  sleep "$T_FAULT"

  # --- recover -------------------------------------------------------------
  for arm in $LOOP_ARMS; do
    info "restore($arm): $(api POST /restore "{\"cluster\":\"$arm\",\"node\":\"all\"}" | head -c 200)"
  done
  echo "$c,$t_ready,$t_inject,$(now_ms)" >> "$CYCLES_CSV"
  # the revived node needs its CNC streams re-pushed against the regenerated
  # MAC before the next fill can pin a placement onto it
  for arm in $LOOP_ARMS; do
    arm_run "$arm" streams >/dev/null 2>&1 || info "WARNING: stream re-provision failed for $arm"
  done
done

info "timeline: results/$RUN/timeline-<arm>.csv + cycles.csv"
info "figure:   ../.venv/bin/python plot.py results/$RUN"
summary
