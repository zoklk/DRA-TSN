#!/usr/bin/env bash
# One-shot driver: run every experiment and render every figure.
# Artifact-evaluation entry point — see README.md for per-experiment prereqs.
#
# Usage:
#   EXPS="placement tagging scale" RUN=paper ./run_all.sh
#   EXPS="scale" ./run_all.sh          # subset (e.g. no real cluster available)
# Env: SSH_PASS (placement/tagging on a password-ssh cluster),
#      TRIALS/FRAMES/NODES_LIST forwarded to the respective run.sh.
set -u
cd "$(dirname "$0")"
: "${EXPS:=placement tagging scale}"
: "${RUN:=$(date +%Y%m%d-%H%M%S)}"
export RUN
PY=.venv/bin/python
[ -x "$PY" ] || { echo "venv missing: python3 -m venv .venv && .venv/bin/pip install matplotlib numpy"; exit 1; }
rc=0

for e in $EXPS; do
  case "$e" in
    placement)
      ./fig_placement/run.sh || rc=1
      "$PY" fig_placement/plot.py "fig_placement/results/$RUN" || rc=1 ;;
    tagging)
      ./fig_tagging/run.sh || rc=1
      ./fig_tagging/parse.sh "fig_tagging/results/$RUN" || rc=1
      "$PY" fig_tagging/plot.py "fig_tagging/results/$RUN" || rc=1 ;;
    scale)
      ./fig_scale/run.sh || rc=1
      "$PY" fig_scale/plot.py "fig_scale/results/$RUN" || rc=1 ;;
    mixed)    # not in the default EXPS: needs the 6-worker kind cluster + docker
              # + the GPU driver (kind/deploy-gpu.sh).
              #
              # fig_mixed has TWO paths and they do not compose into one line.
              # The BATCH path (run.sh) fills, injects once and writes
              # mixed/evictions/util.csv — stage decomposition, no figure. The
              # production-waveform figure is the LIVE path: live_serve.py
              # appends the state timeline while scenario_loop.sh drives the
              # episodes, and plot.py reads cycles.csv + timeline-*.csv.
              # Running the batch collector and then the live plotter (what
              # this case used to do) always failed on the missing timeline.
      ./fig_mixed/run.sh || rc=1
      echo "note: the production figure comes from the live path, not this one."
      echo "      see fig_mixed/README.md §live — live_serve.py + scenario_loop.sh,"
      echo "      then: $PY fig_mixed/plot.py fig_mixed/results/<RUN>" ;;
    regime)   # superseded by `mixed` (ADR-0039); kept so the preserved
              # results/paper, results/paper2 collections stay reproducible.
              # NOTE: kind/kind-config.yaml now ships the tuned detection
              # constants (grace 8s), while results/paper* were collected on
              # kube defaults (~44-52 s detection). A fresh run does NOT
              # reproduce the recorded detect stage.
      ./fig_regime/run.sh || rc=1
      "$PY" fig_regime/plot.py "fig_regime/results/$RUN" || rc=1 ;;
    twin)     # superseded by `regime` (ADR-0037) and then by `mixed`; kept as
              # record. Its collector is run_fault.sh and its feed builder is
              # build_feed.py, so it does not follow the run.sh/plot.py pair
              # this driver assumes (ADR-0029).
      ./fig_twin/run_fault.sh || rc=1
      "$PY" fig_twin/build_feed.py "fig_twin/results/$RUN" || rc=1
      "$PY" fig_twin/plot.py "fig_twin/results/$RUN" || rc=1 ;;
    *) echo "unknown experiment: $e"; rc=1 ;;
  esac
done

echo
echo "=== figures (RUN=$RUN) ==="
find fig_*/results/"$RUN" -name '*.png' 2>/dev/null
exit "$rc"
