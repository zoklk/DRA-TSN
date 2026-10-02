#!/usr/bin/env python3
# Render the scalability figure from results/<RUN>/scale.csv.
#   ../.venv/bin/python plot.py results/<RUN>   (default: latest run dir)
# Output: results/<RUN>/fig_scale.png
# Left : placement correctness (on-TSN-node rate) vs cluster size.
# Right: batch makespan (create -> all scheduled) vs cluster size,
#        baseline (no claim) vs DRATSN (TSN claim).
import os, sys, csv, glob
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
run = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(os.path.join(HERE, "results", "*")))[-1]

BLUE, AQUA, YEL, GRN = "#2a78d6", "#1baf7a", "#eda100", "#008300"
GREY, INK, INK2 = "#8a8a86", "#0b0b0b", "#52514e"
plt.rcParams.update({
    "font.size": 11, "axes.edgecolor": "#cfcfca", "axes.linewidth": 0.8,
    "text.color": INK, "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
    "figure.facecolor": "white", "axes.facecolor": "white",
})

rows = list(csv.DictReader(open(os.path.join(run, "scale.csv"))))
Ns = sorted({int(r["nodes"]) for r in rows})
def col(arm, field):
    return [next(float(r[field]) for r in rows if int(r["nodes"]) == n and r["arm"] == arm) for n in Ns]
pods = int(rows[0]["pods"])

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.4, 3.6))
x = np.arange(len(Ns)); w = 0.36

# --- left: correctness ---
b_rate = [100.0 * v / pods for v in col("baseline", "on_tsn")]
d_rate = [100.0 * v / pods for v in col("dratsn", "on_tsn")]
b1 = ax1.bar(x - w/2, b_rate, w, color=GREY, label="baseline (no claim)")
b2 = ax1.bar(x + w/2, d_rate, w, color=BLUE, label="DRATSN (TSN claim)")
ax1.bar_label(b1, fmt="%.0f%%", fontsize=8.5, color=INK2)
ax1.bar_label(b2, fmt="%.0f%%", fontsize=8.5, color=INK2)
ax1.set_xticks(x, [str(n) for n in Ns])
ax1.set_xlabel("cluster size (fake nodes, KWOK)")
ax1.set_ylabel("pods on TSN-capable nodes (%)")
ax1.set_ylim(0, 135)
ax1.axhline(100, color=INK2, lw=0.8, ls=":")
ax1.set_title(f"Placement correctness ({pods} pods / run)", fontsize=11)
ax1.legend(frameon=False, fontsize=9, loc="upper right")
ax1.grid(axis="y", color="#ececea"); ax1.set_axisbelow(True)
over = {int(r["nodes"]): r["overflow_pending"] for r in rows if r["arm"] == "dratsn"}
note = ("capacity+1 pod stays Pending at every N"
        if all(v == "yes" for v in over.values())
        else "overflow leak at: " + ",".join(str(n) for n, v in over.items() if v != "yes"))
ax1.text(0.02, 0.97, note, transform=ax1.transAxes, va="top", fontsize=8, color=INK2)

# --- right: makespan ---
ax2.plot(Ns, col("baseline", "makespan_ms"), "o-", color=GREY, label="baseline (no claim)")
ax2.plot(Ns, col("dratsn", "makespan_ms"), "o-", color=BLUE, label="DRATSN (TSN claim)")
ax2.set_xscale("log")
ax2.set_xticks(Ns, [str(n) for n in Ns]); ax2.minorticks_off()
ax2.set_xlabel("cluster size (fake nodes, KWOK)")
ax2.set_ylabel("batch makespan (ms)")
ax2.set_ylim(0, max(col("dratsn", "makespan_ms") + col("baseline", "makespan_ms")) * 1.3)
ax2.set_title(f"Create → all {pods} pods scheduled", fontsize=11)
ax2.legend(frameon=False, fontsize=9)
ax2.grid(color="#ececea"); ax2.set_axisbelow(True)

fig.tight_layout()
out = os.path.join(run, "fig_scale.png")
fig.savefig(out, dpi=170, bbox_inches="tight")
print(f"wrote {out}")
