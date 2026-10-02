#!/usr/bin/env python3
# Render figures/f8_cycle_ccdf.png — CCDF of the round-trip control-cycle latency
# from the 1M busy-poll batch run (scripts/results/busy-poll/roundtrip-arm1-iso-1M).
#
# Needs matplotlib + numpy. Run e.g.:
#   python3 -m venv /tmp/venv && /tmp/venv/bin/pip install matplotlib numpy
#   /tmp/venv/bin/python figures/scripts/f8_cycle_ccdf.py
import os, csv, glob, numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
BASE = os.path.join(REPO, "scripts/results/busy-poll/roundtrip-arm1-iso-1M")
OUT  = os.path.join(REPO, "figures")

# palette (dataviz default)
BLUE, AQUA, YEL, GRN = "#2a78d6", "#1baf7a", "#eda100", "#008300"
GREY, INK, INK2 = "#8a8a86", "#0b0b0b", "#52514e"
plt.rcParams.update({
    "font.size": 11, "axes.edgecolor": "#cfcfca", "axes.linewidth": 0.8,
    "text.color": INK, "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
    "figure.facecolor": "white", "axes.facecolor": "white",
})

def load(d, name, cols):
    p = os.path.join(d, name); out = {}
    if not os.path.exists(p): return out
    with open(p) as f:
        for r in csv.DictReader(f):
            try: out[int(r["seq"])] = {c: int(r[c]) for c in cols}
            except (KeyError, ValueError, TypeError): pass  # TypeError = truncated dump line
    return out

pool = {k: [] for k in ("t0t1","t1t2","t2t3","t3t4","cycle")}
for d in sorted(glob.glob(os.path.join(BASE, "batch-*"))):
    send=load(d,"send.csv",["t0_ns"]); recv=load(d,"recv.csv",["t4_ns"])
    tx=load(d,"tx_ts.csv",["t1_ns","t2_ns"]); rx=load(d,"rx_ts.csv",["t3_ns"])
    if not send: continue
    rs=min(v["t0_ns"] for v in send.values()); fr=lambda t: t and t>=rs
    for s in send:
        if s in tx and fr(tx[s]["t1_ns"]): pool["t0t1"].append(tx[s]["t1_ns"]-send[s]["t0_ns"])
    for s in tx:
        if fr(tx[s]["t1_ns"]) and fr(tx[s]["t2_ns"]): pool["t1t2"].append(tx[s]["t2_ns"]-tx[s]["t1_ns"])
    for s in rx:
        if s in tx and fr(tx[s]["t2_ns"]) and fr(rx[s]["t3_ns"]): pool["t2t3"].append(rx[s]["t3_ns"]-tx[s]["t2_ns"])
    for s in recv:
        if s in rx and fr(rx[s]["t3_ns"]): pool["t3t4"].append(recv[s]["t4_ns"]-rx[s]["t3_ns"])
    for s in recv:
        if s in send: pool["cycle"].append(recv[s]["t4_ns"]-send[s]["t0_ns"])

A = {k: np.array([x for x in v if x is not None and x>=0], float)/1000.0 for k,v in pool.items()}
def pct(a,p): a=np.sort(a); return a[min(len(a)-1,int(round(p/100.0*(len(a)-1))))]
print("N cycle=", len(A["cycle"]), " per-stage n=", {k:len(A[k]) for k in A})

# ---------- CCDF of CYCLE (absolute/linear x, 1ms + 20% lines) ----------
fig, ax = plt.subplots(figsize=(9.0,5.0))
c = np.sort(A["cycle"]); n=len(c)
ccdf = 1.0 - (np.arange(1,n+1))/n
ranks = np.unique(np.concatenate([
    np.geomspace(1, n-1, 2500).astype(int),
    np.arange(n-2000, n-1)  # dense tail
]))
ranks = ranks[(ranks>=0)&(ranks<n-1)]
ax.plot(c[ranks], ccdf[ranks], color=BLUE, lw=2.4, solid_capstyle="round", zorder=5)

over1000 = int((c>1000).sum()); over200 = int((c>200).sum())
print(f"cycle>1000us: {over1000}/{n}   cycle>200us: {over200}/{n} ({over200/n:.2e})")

# threshold lines: 1ms budget + 20%*1ms = 200us
ax.axvline(1000, color="#444", ls="-", lw=2.0, zorder=3)
ax.text(975, 1.9, "1 ms\nIEEE isochronous\ncycle budget", color=INK, fontsize=9, ha="right", va="top")
ax.axvline(200, color=GREY, ls="--", lw=1.6, zorder=3)
ax.text(206, 1.9, "200 µs\n20% × 1 ms\n(transfer budget)", color=INK2, fontsize=8.6, ha="left", va="top")

for p,lv,lab in [(50,0.5,"p50"),(99,1e-2,"p99"),(99.9,1e-3,"p99.9"),(99.99,1e-4,"p99.99"),(99.999,1e-5,"p99.999")]:
    xv=pct(A["cycle"],p)
    ax.plot([xv],[lv],"o",color=BLUE,ms=7,mec="white",mew=1.2,zorder=6)
    ax.annotate(f"{lab} = {xv:.0f}µs", (xv,lv), textcoords="offset points", xytext=(9,0),
                fontsize=8.4, color=INK, va="center")
mx=c.max()
ax.annotate(f"max = {mx:.0f}µs", (mx,1.0/n), textcoords="offset points", xytext=(9,2), fontsize=8.4, color=INK2)

ax.set_yscale("log")               # linear x = absolute latency, log y = show five-nines tail
ax.set_xlim(0, 1030); ax.set_ylim(6e-7, 1.6)
ax.set_xlabel("round-trip cycle latency  (µs)"); ax.set_ylabel("P(cycle > x)  (log)")
ax.grid(True, which="major", color="#ececea", lw=0.7, zorder=0)
ax.grid(True, which="minor", axis="y", color="#f5f5f3", lw=0.5, zorder=0)
ax.set_title("Round-trip control-cycle latency", fontsize=12.5, pad=42, loc="center")
fig.tight_layout()
fig.savefig(os.path.join(OUT,"f8_cycle_ccdf.png"), dpi=170, bbox_inches="tight")
print("wrote", os.path.join(OUT,"f8_cycle_ccdf.png"))
