#!/usr/bin/env python3
"""fig_mixed figure — good parts per minute over time (ADR-0039 §8).

Input is the state timeline that live_serve appends while it paints the scene
(`timeline-<arm>.csv`), plus the cycle windows the loop driver records
(`cycles.csv`). One source for both the figure and the animation, so they cannot
disagree.

WHAT IS MEASURED AND WHAT IS DECLARED — the boundary this file exists to keep
(ADR-0031, ADR-0036):

    measured   which node each workload runs on, whether that node still
               publishes a live stream for it, and when each transition happened
    declared   the per-cycle wire-delay distribution, the run threshold, and the
               cell's assembly time

The y value is a composition of the two, never a measurement. A measured state
timeline selects a path class; the declared model turns that class into a defect
rate; the product is the curve. `sim_model` is imported rather than reimplemented
precisely so the figure and the scene's delay gauge share one model.

The vanilla arm's spread across cycles IS the result. Plotting only the mean
would erase the finding.
"""
import csv
import os
import sys
import importlib.util

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# --- declared constants ------------------------------------------------------
# One part per assembly cycle. Declared, like every other value on the y axis;
# it only scales the curve and is stated in the caption.
CELL_CYCLE_S = 4.0
PARTS_PER_MIN = 60.0 / CELL_CYCLE_S
# Declared median of the unprotected wire delay (ms). Chosen inside the model's
# transition band: below ~0.3 ms an unprotected path is indistinguishable from a
# protected one over a 4 s part, above ~1.0 ms it produces nothing at all and the
# curve collapses onto the stopped line. The band is narrow because a run of
# `RUN_THRESHOLD` consecutive misses over thousands of control cycles is close to
# a step function — which is exactly why ADR-0036 sweeps it rather than asserting
# one value (isaac-sim/sweep.py).
LAM_MS = 0.7

# palette shared with the other figures
BLUE, GREY, RED = "#2a78d6", "#8a8a86", "#c0392b"
COLOUR = {"dratsn": BLUE, "vanilla": RED}
# Figures stay in English (repo convention, and the plotting font has no Hangul).
LABEL = {"dratsn": "DRA-TSN", "vanilla": "vanilla K8s (static labels)"}
# TODO (ADR-0039 §8): the declared physical-PLC track is not drawn yet. It is a
# reference line, not a measured arm, so it needs a declared repair time rather
# than a timeline.


def _load_sim_model():
    """Locate sim_model.py, which is not an installed package.

    Two layouts have to work. In the development workspace isaac-sim is a
    SIBLING of the repo root (TSN-dra/{dratsn,isaac-sim}); in the published
    repo the driver is the root and isaac-sim sits UNDER it. Trying both keeps
    the figure reproducible across the move instead of breaking on the first
    clone.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "..", "isaac-sim", "sim_model.py"),       # published: repo/isaac-sim
        os.path.join(here, "..", "..", "..", "isaac-sim", "sim_model.py"),  # dev: sibling of repo
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        raise SystemExit("sim_model.py not found; looked in: " +
                         ", ".join(os.path.abspath(p) for p in candidates))
    spec = importlib.util.spec_from_file_location("sim_model", os.path.abspath(path))
    if spec is None or spec.loader is None:
        raise SystemExit(f"sim_model.py not loadable at {path}")
    mod = importlib.util.module_from_spec(spec)
    # register before exec: sim_model defines dataclasses, and @dataclass
    # resolves annotations through sys.modules[cls.__module__]
    sys.modules["sim_model"] = mod
    spec.loader.exec_module(mod)
    return mod


SM = _load_sim_model()


def rate_for(path_ok: bool) -> float:
    """Good parts per minute for a protected / unprotected control path.

    protected   the GCL bounds the delay, so no deadline is ever missed
    unprotected the declared lognormal produces misses; a run of them long
                enough to break the trajectory spoils the part
    """
    # Control cycles inside ONE part, not inside the scene's compressed dwell:
    # this axis is parts per minute of real production.
    n = int(CELL_CYCLE_S * 1000.0 / SM.T_CYCLE_MS)
    if path_ok:
        return PARTS_PER_MIN
    q = SM.miss_rate(LAM_MS)
    return PARTS_PER_MIN * (1.0 - SM.defect_prob(q, n))


P_OK, P_BAD = rate_for(True), rate_for(False)


# --- inputs ------------------------------------------------------------------

def load_timeline(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        so = r["stream_ok"]
        out.append((int(r["t_ms"]), r["state"],
                    None if so == "" else int(so)))
    return out


def load_cycles(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [{k: int(v) for k, v in r.items()} for r in csv.DictReader(f)]


def rate_series(tl, t_from, t_to, step_ms=500):
    """Sample the production rate over [t_from, t_to].

    A cell is producing only while its control loop is running; between the
    fault and the replacement pod becoming ready it makes nothing. Once it is
    running again the rate depends on whether the new node actually has a path —
    which is the entire point of the experiment and the only thing that differs
    between the arms.
    """
    ts = np.arange(t_from, t_to, step_ms)
    ys = np.zeros_like(ts, dtype=float)
    i = 0
    state, ok = "down", None
    for j, t in enumerate(ts):
        while i < len(tl) and tl[i][0] <= t:
            _, state, so = tl[i]
            if so is not None:
                ok = so
            i += 1
        if state in ("running", "running_again"):
            ys[j] = P_OK if ok == 1 else P_BAD
        else:
            ys[j] = 0.0
    return (ts - t_from) / 1000.0, ys


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else "results/live"
    cycles = load_cycles(os.path.join(run, "cycles.csv"))
    if not cycles:
        raise SystemExit(f"no cycles.csv in {run} — run scenario_loop.sh first")

    arms = []
    for arm in ("vanilla", "dratsn"):
        p = os.path.join(run, f"timeline-{arm}.csv")
        if os.path.exists(p):
            arms.append((arm, load_timeline(p)))
    if not arms:
        raise SystemExit(f"no timeline-*.csv in {run}")

    fig, ax = plt.subplots(figsize=(9, 4.2))

    # Each cycle is an independent episode: measured from the moment the fill
    # settled to the moment the node was restored. The rebuild between cycles is
    # an experiment reset, not an event, so it is cut out and the episodes are
    # laid end to end. Stated in the caption.
    # Every cycle drawn solid, laid end to end: a wave train, not an average.
    # A mean line would sit on the first cycle's x range and read as "cycle 1
    # emphasised", and averaging is wrong here anyway — the spread between
    # cycles IS the finding, and one arm's single correct landing would vanish
    # into a mean.
    bounds = []
    for arm, tl in arms:
        offset = 0.0
        for i, c in enumerate(cycles):
            t, y = rate_series(tl, c["t_ready_ms"], c["t_restore_ms"])
            if not len(t):
                continue
            ax.plot(t + offset, y, color=COLOUR[arm], lw=1.8,
                    label=LABEL[arm] if i == 0 else None)
            if arm == arms[0][0]:
                bounds.append((offset, offset + t[-1]))
            offset += t[-1] + 1

    # cycle separators + numbers, so a reader can count the episodes
    for i, (a, b) in enumerate(bounds):
        if i:
            ax.axvline(a - 0.5, color="#d8d8d4", lw=0.8, zorder=0)
        ax.text((a + b) / 2, PARTS_PER_MIN * 1.09, f"cycle {i + 1}",
                ha="center", va="center", fontsize=8, color=GREY)

    # the two rates the declared model produces, as reference lines
    ax.axhline(P_OK, color=GREY, ls=":", lw=1.2)
    ax.text(0.995, P_OK, " protected path", color=GREY, fontsize=8,
            va="bottom", ha="right", transform=ax.get_yaxis_transform())
    ax.axhline(P_BAD, color=GREY, ls=":", lw=1.2)
    ax.text(0.995, P_BAD, " best-effort path", color=GREY, fontsize=8,
            va="bottom", ha="right", transform=ax.get_yaxis_transform())

    ax.set_xlabel("elapsed time per cycle (s) — inter-cycle rebuild excised")
    ax.set_ylabel("good parts / min")
    ax.set_ylim(0, PARTS_PER_MIN * 1.15)
    ax.legend(loc="lower right", fontsize=9, framealpha=0.95)
    ax.grid(alpha=0.25, lw=0.5)
    ax.set_title("Production after path loss — one trace per cycle", fontsize=11)
    fig.text(0.01, 0.01,
             f"placement and transitions measured; output converted through the "
             f"declared model (ADR-0036, median={LAM_MS} ms, "
             f"control cycle {SM.T_CYCLE_MS} ms, part {CELL_CYCLE_S} s) — not a new measurement",
             fontsize=7, color=GREY)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    out = os.path.join(run, "f_mixed_production.png")
    fig.savefig(out, dpi=160)
    print(f"wrote {out}   P_ok={P_OK:.1f}/min  P_bad={P_BAD:.1f}/min")


if __name__ == "__main__":
    main()
