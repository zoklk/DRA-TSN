#!/usr/bin/env python3
# Render fig_tagging figures. All follow the (a) without / (b) with ablation of
# the TSN metadata proxy paper (arXiv:2503.12878 Fig.2 — figures/f11_example.png).
#
#   ../.venv/bin/python plot.py results/<RUN>   (default: latest run dir)
# Outputs (rendered when their inputs exist in the run dir):
#   fig_tagging.png         TX tagging classification (run.sh + parse.sh)
#   fig_tagging_rx.png      RX frame fate            (run.sh + parse.sh)
#   fig_tagging_taprio.png  taprio cycle scatter     (run_taprio.sh + parse_taprio.sh)
import os, sys, csv, glob
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

HERE = os.path.dirname(os.path.abspath(__file__))
run = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(os.path.join(HERE, "results", "*")))[-1]

BLUE, AQUA, YEL, GRN = "#2a78d6", "#1baf7a", "#eda100", "#008300"
ORANGE = "#f0a83a"; PERI = "#8890dd"   # example-figure palette (listener #1/#2)
GREY, INK, INK2 = "#8a8a86", "#0b0b0b", "#52514e"
plt.rcParams.update({
    "font.size": 10.5, "axes.edgecolor": "#cfcfca", "axes.linewidth": 0.8,
    "text.color": INK, "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
    "figure.facecolor": "white", "axes.facecolor": "white",
})
OFF = {"ot": 0.14, "it": -0.14}


def render_main(meta, rows, n):
    """TX classification: x = capture time, y = observed PCP lane."""
    TXPCP = int(meta["TX_PCP"])
    UNTAG_Y = -1.5

    def panel(ax, side, subtitle):
        tmax = 0.0
        for cls, color in (("ot", PERI), ("it", ORANGE)):
            sel = [r for r in rows if r["side"] == side and r["class"] == cls]
            if not sel:
                continue
            t = np.array([float(r["t_rel"]) for r in sel])
            y = np.array([int(r["pcp"]) if r["pcp"] else UNTAG_Y for r in sel], dtype=float)
            ax.plot(t, y + OFF[cls], "o", ms=3.0, alpha=0.6, color=color, mew=0)
            tmax = max(tmax, t.max())
        ax.axhspan(TXPCP - 0.4, TXPCP + 0.4, color=BLUE, alpha=0.07, lw=0)
        ax.axhline(TXPCP, color=BLUE, lw=1.0, alpha=0.5)
        ax.axhline(UNTAG_Y, color=GRN, lw=1.0, alpha=0.5)
        ax.set_ylim(UNTAG_Y - 1.0, 7.8)
        ax.set_yticks([UNTAG_Y, 0, 1, 2, 3, 4, 5, 6, 7],
                      ["untagged\n(best-effort)", "0", "1", "2", "3", "4", "5", "6", "7"])
        ax.set_ylabel("observed 802.1Q PCP")
        ax.set_xlim(-0.1, tmax * 1.28 if tmax else 1)
        ax.grid(color="#ececea", ls=":"); ax.set_axisbelow(True)
        ax.set_title(subtitle, fontsize=10.5, pad=6)
        ax.text(tmax * 1.26, TXPCP, f"TSN lane\nVLAN {meta['TX_VLAN']}/PCP {TXPCP}",
                ha="right", va="center", fontsize=8, color=BLUE)

    fig, (axa, axb) = plt.subplots(2, 1, figsize=(7.0, 6.2))
    panel(axa, "txbase", "(a) Without DRATSN (pod without ResourceClaim)")
    panel(axb, "tx",     "(b) DRATSN enabled (claimed TSN stream)")
    axa.legend(handles=[
        Line2D([], [], marker="o", ls="", color=PERI, label=f"OT flow → :4840 (registered), N={n}"),
        Line2D([], [], marker="o", ls="", color=ORANGE, label=f"IT flow → :9999 (map miss), N={n}"),
    ], frameon=True, framealpha=0.95, edgecolor="#cfcfca", fontsize=8.4, loc="center right")
    axb.set_xlabel("frame capture time on eno1 egress (s)")
    fig.tight_layout()
    out = os.path.join(run, "fig_tagging.png")
    fig.savefig(out, dpi=170, bbox_inches="tight")
    print(f"wrote {out}")


def render_rx(meta, lat_rows, n):
    """RX pod delivery: x = injected frame seq, y = XDP(t3) -> pod-socket latency.
    Undelivered frames sit in the 'not delivered' band at the top."""
    delivered = [float(r["lat_us"]) for r in lat_rows if r["delivered"] == "1"]
    ymax = max(delivered) * 1.25 if delivered else 100.0
    BAND = ymax * 1.18  # not-delivered band center

    def rx_panel(ax, side, subtitle):
        for cls, color in (("ot", PERI), ("it", ORANGE)):
            sel = [r for r in lat_rows if r["side"] == side and r["class"] == cls]
            ok = [(int(r["seq"]), float(r["lat_us"])) for r in sel if r["delivered"] == "1"]
            no = [int(r["seq"]) for r in sel if r["delivered"] != "1"]
            if ok:
                ax.plot([s for s, _ in ok], [l for _, l in ok], "o", ms=3.2,
                        alpha=0.65, color=color, mew=0)
            if no:
                jit = BAND + (np.array(no) % 7 - 3) * ymax * 0.008
                ax.plot(no, jit, "x", ms=3.5, alpha=0.5, color=color, mew=1.1)
            cnt_ok, cnt_no = len(ok), len(no)
            y = 0.30 if cls == "ot" else 0.16
            ax.text(0.99, y, f"{cls.upper()}: delivered {cnt_ok}/{cnt_ok + cnt_no}",
                    transform=ax.transAxes, ha="right", fontsize=8.5, color=color)
        ax.axhspan(BAND - ymax * 0.05, BAND + ymax * 0.05, color="#f3f3f0", lw=0)
        ax.axhline(BAND, color=GREY, lw=0.6, ls=":")
        ax.set_ylim(0, BAND + ymax * 0.09)
        ax.set_yticks(list(ax.get_yticks())[:-1] + [BAND])
        labels = [f"{v:g}" for v in ax.get_yticks()[:-1]] + ["not\ndelivered"]
        ax.set_yticklabels(labels)
        ax.set_ylabel("XDP entry → pod socket (µs)")
        ax.grid(axis="y", color="#ececea", ls=":"); ax.set_axisbelow(True)
        ax.set_title(subtitle, fontsize=10.5, pad=6)

    fig2, (axc, axd) = plt.subplots(2, 1, figsize=(7.0, 6.0), sharex=True)
    rx_panel(axc, "rxbase", "(a) Without DRATSN (rx_flows empty — no claimed pod)")
    rx_panel(axd, "rx",     "(b) DRATSN enabled (claimed TSN stream)")
    if delivered:
        med = float(np.median(delivered))
        axd.axhline(med, color=BLUE, lw=1.0, alpha=0.6)
        axd.text(0.005, med * 1.06, f"p50 {med:.0f} µs", transform=axd.get_yaxis_transform(),
                 fontsize=8.5, color=BLUE)
        axd.text(0.99, 0.44, f"DNAT {meta['RX_ENDPOINT_IP']} → {meta['POD_IP']}",
                 transform=axd.transAxes, ha="right", fontsize=8.5, color=INK2)
    axc.legend(handles=[
        Line2D([], [], marker="o", ls="", color=PERI, label=f"OT: talker {meta['ARM1_IP']} (rx_flows hit), N={n}"),
        Line2D([], [], marker="o", ls="", color=ORANGE, label=f"IT: talker {meta['IT_TALKER']} (miss), N={n}"),
        Line2D([], [], marker="x", ls="", color=GREY, label="not delivered to any pod"),
    ], frameon=True, framealpha=0.95, edgecolor="#cfcfca", fontsize=8.2, loc="center right")
    axd.set_xlabel("injected frame seq (OT even / IT odd, interleaved)")
    fig2.tight_layout()
    out2 = os.path.join(run, "fig_tagging_rx.png")
    fig2.savefig(out2, dpi=170, bbox_inches="tight")
    print(f"wrote {out2}")


def render_taprio(tp, tprows):
    """taprio cycle scatter: x = release offset within cycle, y = cycle #
    (tcpdump egress tap = post-qdisc, so timestamps are gate-release times)."""
    cyc_ms = int(tp["CYCLE_NS"]) / 1e6
    gate_ms = int(tp["TSN_GATE_NS"]) / 1e6

    def tp_panel(ax, side, subtitle):
        sel_all = [r for r in tprows if r["side"] == side]
        cyc0 = min((int(r["cycle_idx"]) for r in sel_all), default=0)
        for cls, color in (("ot", PERI), ("it", ORANGE)):
            sel = [r for r in sel_all if r["class"] == cls]
            if sel:
                ax.plot([float(r["offset_us"]) / 1000 for r in sel],
                        [int(r["cycle_idx"]) - cyc0 for r in sel],
                        "o", ms=3.4, alpha=0.6, color=color, mew=0)
        ax.axvline(0, color=BLUE, lw=1.8)
        ax.axvline(gate_ms, color=GRN, lw=1.8)
        ax.axvspan(0, gate_ms, color=BLUE, alpha=0.05, lw=0)
        ax.set_xlim(-0.3, cyc_ms)
        ax.set_ylabel("taprio cycle #")
        ax.grid(color="#ececea", ls=":"); ax.set_axisbelow(True)
        ax.set_title(subtitle, fontsize=10.5, pad=6)

    fig3, (axe, axf) = plt.subplots(2, 1, figsize=(7.0, 6.2))
    tp_panel(axe, "base", "(a) Without DRATSN (pod without ResourceClaim)")
    tp_panel(axf, "mix",  "(b) DRATSN enabled (skb->priority from claimed stream)")
    nf = int(tp["FRAMES"])
    axe.legend(handles=[
        Line2D([], [], marker="o", ls="", color=PERI, label=f"OT flow → :4840, N={nf}"),
        Line2D([], [], marker="o", ls="", color=ORANGE, label=f"IT flow → :9999, N={nf}"),
        Line2D([], [], color=BLUE, lw=1.8, label=f"TSN gate opens (prio {tp['TX_PCP']}, {gate_ms:g} ms window)"),
        Line2D([], [], color=GRN, lw=1.8, label="best-effort gate opens"),
    ], frameon=True, framealpha=0.95, edgecolor="#cfcfca", fontsize=8.2, loc="center right")
    axf.set_xlabel(f"frame release offset within taprio cycle (ms, cycle = {cyc_ms:g} ms)")
    fig3.tight_layout()
    out3 = os.path.join(run, "fig_tagging_taprio.png")
    fig3.savefig(out3, dpi=170, bbox_inches="tight")
    print(f"wrote {out3}")


if os.path.exists(os.path.join(run, "meta.env")) and os.path.exists(os.path.join(run, "packets.csv")):
    meta = dict(l.strip().split("=", 1) for l in open(os.path.join(run, "meta.env")) if "=" in l)
    rows = list(csv.DictReader(open(os.path.join(run, "packets.csv"))))
    n = int(meta["FRAMES"])
    render_main(meta, rows, n)
    lat_path = os.path.join(run, "rx_latency.csv")
    if os.path.exists(lat_path):
        render_rx(meta, list(csv.DictReader(open(lat_path))), n)

if os.path.exists(os.path.join(run, "taprio.env")) and os.path.exists(os.path.join(run, "taprio_packets.csv")):
    tp = dict(l.strip().split("=", 1) for l in open(os.path.join(run, "taprio.env")) if "=" in l)
    tprows = list(csv.DictReader(open(os.path.join(run, "taprio_packets.csv"))))
    render_taprio(tp, tprows)
