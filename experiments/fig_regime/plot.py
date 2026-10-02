#!/usr/bin/env python3
# Render the tight-regime failover figures from results/<RUN>/{regime,evictions,util}.csv.
#   ../.venv/bin/python plot.py results/<RUN>   (default: latest run dir)
# Output: results/<RUN>/f15_recovery.png, results/<RUN>/f16_plane.png
#
# f15  stage-decomposed recovery per arm (detect / evict / schedule / start) and
#      where the vPLC lands. The detect stage is dominated by the kube default
#      node-monitor-grace-period and is shown, not hidden: honesty about what the
#      scheduler can and cannot make faster (ADR-0037).
# f16  the utilisation x availability plane. Three strategies, three corners —
#      spare buys availability with an idle node, vanilla buys utilisation with
#      blind placement, dratsn takes both. This is the figure of the claim.
#
# Palette matches fig_twin/fig_placement so the evaluation reads as one set.
# Grey stays the untreated reference (vanilla); spare gets a light blue, the
# stage segments are a single-hue ramp because they are parts of one duration.
import os, sys, csv, glob
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
run = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(os.path.join(HERE, "results", "*")))[-1]

BLUE, GREY, INK, INK2 = "#2a78d6", "#8a8a86", "#0b0b0b", "#52514e"
LIGHTBLUE = "#8fb8e8"
STAGE_COLOURS = ["#c9dcf5", "#8fb8e8", "#5493dd", "#2a78d6"]   # one hue, four depths
plt.rcParams.update({
    "font.size": 11, "axes.edgecolor": "#cfcfca", "axes.linewidth": 0.8,
    "text.color": INK, "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
    "figure.facecolor": "white", "axes.facecolor": "white",
})

def load(name):
    path = os.path.join(run, name)
    return list(csv.DictReader(open(path))) if os.path.exists(path) else []

rows = load("regime.csv")
ev = load("evictions.csv")
util = load("util.csv")
ARMS = ["spare", "vanilla", "dratsn"]
LABEL = {"spare": "spare node", "vanilla": "vanilla + priority", "dratsn": "DRATSN + priority"}
COLOUR = {"spare": LIGHTBLUE, "vanilla": GREY, "dratsn": BLUE}
arms = [a for a in ARMS if any(r["arm"] == a for r in rows)]

def arm_rows(a):
    # vPLC recoveries only: fixture-v2 rows include the camfeed gateway, whose
    # correct landing is a general node — mixing it in would halve the landing
    # panel (and pollute the stage medians) for no reason
    return [r for r in rows if r["arm"] == a and r["vplc"].startswith("vplc")]

def med(vals):
    return float(np.median(vals)) if vals else 0.0

# --- f15: recovery decomposition + landing ----------------------------------
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(6.8, 6.2),
                               gridspec_kw={"height_ratios": [1.0, 1.0]})

STAGES = ["detect", "evict", "schedule", "start"]
y = np.arange(len(arms))
for i, a in enumerate(arms):
    ok = [r for r in arm_rows(a) if r["pending"] == "0"]
    if not ok:
        continue
    cum = [med([float(r["detect_ms"]) for r in ok]),
           med([float(r["evict_ms"]) for r in ok]),
           med([float(r["sched_ms"]) for r in ok]),
           med([float(r["ready_ms"]) for r in ok])]
    left = 0.0
    for s, (name, colour) in enumerate(zip(STAGES, STAGE_COLOURS)):
        width = max(cum[s] - left, 0.0)
        ax1.barh(i, width / 1000.0, left=left / 1000.0, color=colour,
                 label=name if i == len(arms) - 1 else None, height=0.55)
        left = cum[s]
    ax1.text(left / 1000.0 + 0.4, i, f"{left/1000.0:.1f}s median",
             va="center", fontsize=9, color=INK2)

ax1.set_yticks(y, [LABEL[a] for a in arms])
ax1.set_xlabel("time from node death (s), median over vPLC recoveries")
ax1.set_title("Recovery, stage-decomposed", fontsize=11)
ax1.legend(frameon=False, fontsize=9, loc="upper center", ncols=4,
           bbox_to_anchor=(0.5, -0.28),
           title="detect = node NotReady, kube default grace", title_fontsize=8)
ax1.grid(axis="x", color="#ececea"); ax1.set_axisbelow(True)

# landing: the panel that separates the arms — recovery speed alone does not
cats = ["on a TSN node", "off the TSN path", "pending"]
x = np.arange(len(cats)); w = 0.26
for i, a in enumerate(arms):
    rs = arm_rows(a)
    on = sum(1 for r in rs if r["on_tsn1"] == "1" and r["pending"] == "0")
    off = sum(1 for r in rs if r["on_tsn1"] == "0" and r["pending"] == "0")
    pend = sum(1 for r in rs if r["pending"] == "1")
    bars = ax2.bar(x + (i - 1) * w, [on, off, pend], w, color=COLOUR[a],
                   label=f"{LABEL[a]}, N={len(rs)}")
    ax2.bar_label(bars, fmt="%d", fontsize=9, color=INK2)
ax2.set_xticks(x, cats)
ax2.set_ylabel("vPLC recoveries")
ax2.set_title("Where the vPLC lands", fontsize=11)
ax2.legend(frameon=False, fontsize=9)
ax2.grid(axis="y", color="#ececea"); ax2.set_axisbelow(True)
n_max = max((len(arm_rows(a)) for a in arms), default=1)
ax2.set_ylim(0, n_max * 1.45)

# eviction correctness, stated as text: a bar of "2" would dwarf the point
lines = []
for a in arms:
    evs = [e for e in ev if e["arm"] == a]
    trials = {r["trial"] for r in arm_rows(a)} or {"1"}
    per = len(evs) / len(trials)
    tiers = sorted({e["priority_class"] for e in evs})
    lines.append(f"{LABEL[a]}: {per:.1f} evictions/trial"
                 + (f" ({', '.join(tiers)})" if tiers else " (none)"))
ax2.text(0.02, 0.97, "\n".join(lines), transform=ax2.transAxes,
         va="top", fontsize=8.5, color=INK2)

fig.tight_layout()
out1 = os.path.join(run, "f15_recovery.png")
fig.savefig(out1, dpi=170, bbox_inches="tight")
print(f"wrote {out1}")

# --- f16: the utilisation x availability plane ------------------------------
# x = pre-fault requested share of the whole worker pool (both pools summed):
# an idle spare node is capacity paid for, so it belongs in the denominator.
# y = control availability: share of vPLC recoveries that land back on a TSN
# node (off-TSN = deterministic control lost even though the pod is Running).
fig2, ax = plt.subplots(figsize=(5.6, 5.0))

for a in arms:
    pre = [u for u in util if u["arm"] == a and u["phase"] == "pre"]
    by_trial = {}
    for u in pre:
        t = by_trial.setdefault(u["trial"], {"req": 0, "alloc": 0})
        t["req"] += int(u["requests_mcpu"]); t["alloc"] += int(u["allocatable_mcpu"])
    xs = [100.0 * t["req"] / t["alloc"] for t in by_trial.values() if t["alloc"]]
    rs = arm_rows(a)
    avail = 100.0 * sum(1 for r in rs if r["on_tsn1"] == "1" and r["pending"] == "0") / len(rs) if rs else 0.0
    ux = med(xs)
    ax.scatter([ux], [avail], s=180, color=COLOUR[a], zorder=3,
               edgecolor="white", linewidth=1.5)
    ok = [float(r["ready_ms"]) / 1000.0 for r in rs if r["pending"] == "0" and r["on_tsn1"] == "1"]
    note = f"{LABEL[a]}\n" + (f"recovered in {med(ok):.0f}s" if ok else "no TSN recovery")
    # spare and dratsn both sit at 100% availability; annotate spare to the
    # left so the two labels cannot collide
    left = a == "spare"
    ax.annotate(note, (ux, avail), fontsize=9, color=INK2,
                xytext=(-10 if left else 10, -4), textcoords="offset points",
                ha="right" if left else "left")

ax.set_xlabel("pre-fault cluster utilisation (% of requested cpu)")
ax.set_ylabel("control availability after node death (% of vPLC on TSN)")
ax.set_title("Utilisation × availability: preemption breaks the trade-off", fontsize=11)
ax.set_xlim(0, 100); ax.set_ylim(-5, 110)
ax.grid(color="#ececea"); ax.set_axisbelow(True)

fig2.tight_layout()
out2 = os.path.join(run, "f16_plane.png")
fig2.savefig(out2, dpi=170, bbox_inches="tight")
print(f"wrote {out2}")
