#!/usr/bin/env python3
# Render the placement-correctness figure from results/<RUN>/placement.csv.
#   ../.venv/bin/python plot.py results/<RUN>   (default: latest run dir)
# Output: results/<RUN>/fig_placement.png
# Per-node landing counts, baseline vs DRATSN (grouped bars).
# (Scheduling latency is also recorded in the CSV but not plotted — the
#  client-side ~100ms resolution only supports a "no visible overhead" claim.)
import os, sys, csv, glob
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
run = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(os.path.join(HERE, "results", "*")))[-1]

# palette (dataviz default, matches figures/scripts/f8_cycle_ccdf.py)
BLUE, AQUA, YEL, GRN = "#2a78d6", "#1baf7a", "#eda100", "#008300"
GREY, INK, INK2 = "#8a8a86", "#0b0b0b", "#52514e"
plt.rcParams.update({
    "font.size": 11, "axes.edgecolor": "#cfcfca", "axes.linewidth": 0.8,
    "text.color": INK, "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
    "figure.facecolor": "white", "axes.facecolor": "white",
})

rows = list(csv.DictReader(open(os.path.join(run, "placement.csv"))))
arms = ["baseline", "dratsn"]
# Always show every worker node, even with zero landings. EXP_NODES names the
# pool; it has no default, because a default from one testbed silently draws
# phantom zero-bars (and a 0% on-TSN rate) when replotting a run collected
# somewhere else.
_pool = os.environ.get("EXP_NODES", "")
nodes = sorted({r["node"] for r in rows} |
               {n for n in _pool.split(",") if n})
counts = {a: {n: 0 for n in nodes} for a in arms}
for r in rows:
    counts[r["arm"]][r["node"]] += 1
trials = {a: sum(counts[a].values()) for a in arms}
tsn_node = os.environ.get("TSN_NODE", "")
if not tsn_node:
    raise SystemExit("TSN_NODE must name the TSN-capable node of the run being "
                     "plotted (no default: the on-TSN rate is meaningless "
                     "without it). e.g. TSN_NODE=alpha-w1")

fig, ax1 = plt.subplots(figsize=(6.0, 3.6))

x = np.arange(len(nodes)); w = 0.36
b1 = ax1.bar(x - w/2, [counts["baseline"][n] for n in nodes], w,
             color=GREY, label=f"baseline (no claim), N={trials['baseline']}")
b2 = ax1.bar(x + w/2, [counts["dratsn"][n] for n in nodes], w,
             color=BLUE, label=f"DRATSN (TSN claim), N={trials['dratsn']}")
for bars in (b1, b2):
    ax1.bar_label(bars, fmt="%d", fontsize=9, color=INK2)
ax1.set_xticks(x, [n + ("\n(TSN NIC)" if n == tsn_node else "") for n in nodes])
ax1.set_ylabel("pods scheduled")
ax1.set_title("Pod placement by node", fontsize=11)
ax1.legend(frameon=False, fontsize=9)
ax1.set_ylim(0, max(trials.values()) * 1.42)
ax1.grid(axis="y", color="#ececea"); ax1.set_axisbelow(True)

corr = {a: 100.0 * counts[a].get(tsn_node, 0) / trials[a] for a in arms}
ax1.text(0.02, 0.98, f"on-TSN-node rate\nbaseline {corr['baseline']:.0f}%  /  DRATSN {corr['dratsn']:.0f}%",
         transform=ax1.transAxes, va="top", fontsize=9, color=INK2)

fig.tight_layout()
out = os.path.join(run, "fig_placement.png")
fig.savefig(out, dpi=170, bbox_inches="tight")
print(f"wrote {out}")
print(f"correctness: baseline {corr['baseline']:.1f}% dratsn {corr['dratsn']:.1f}% "
      f"(N={trials['baseline']}/{trials['dratsn']})")
