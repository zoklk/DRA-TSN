#!/usr/bin/env python3
"""Declared-parameter sweep for the DRATSN twin (ADR-0036).

Writes two CSVs next to each other, so both the curve and the animated run come
from the same model:

  sweep.csv    track x median x sigma x threshold -> defect rate (analytic, no seed)
  cycles.csv   per-cycle verdicts at one chosen median/sigma (drives the video)

A single declared value would put its own value up for debate, so every declared
parameter of the unprotected-path lognormal (median, sigma) is swept together
with RUN_THRESHOLD, and the claim is stated as a shape: dratsn stays at zero
over the whole grid, vanilla rises, and the two never cross.

Usage:
  python3 sweep.py [--feed feed.json] [--out results/<run>] [--lambda 0.5] [--sigma 1.0]
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import sim_model as M


MEDIANS = [round(0.05 * 1.25**i, 4) for i in range(31)]  # 0.05 .. ~40 ms, log spaced
SIGMAS = [0.5, 1.0, 1.5]
THRESHOLDS = [3, 4, 6, 10]


def write_sweep(feed: M.Feed, out_dir: Path) -> Path:
    path = out_dir / "sweep.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["track", "median_ms", "sigma", "threshold", "produced", "stopped", "defect", "defect_rate"])
        for track in feed.tracks:
            for sigma in SIGMAS:
                for threshold in THRESHOLDS:
                    for med in MEDIANS:
                        s = M.expected_summary(track, med, threshold, sigma=sigma)
                        w.writerow([
                            track.name, med, sigma, threshold,
                            s["produced"], s["stopped"],
                            f"{s['defect']:.6f}", f"{s['defect_rate']:.6f}",
                        ])
    return path


def write_cycles(feed: M.Feed, out_dir: Path, lam: float, sigma: float, seed: int) -> Path:
    path = out_dir / "cycles.csv"
    runs = M.run_feed(feed, lam, seed=seed, sigma=sigma)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["track", "cycle", "frame_start", "t_ms", "running", "path_class",
                    "assembled", "worst_run", "cum_output", "cum_defect"])
        for name, verdicts in runs.items():
            good = bad = 0
            for v in verdicts:
                if v.running:
                    good += int(v.assembled)
                    bad += int(not v.assembled)
                w.writerow([name, v.cycle, v.frame_start, f"{v.t_ms:.0f}",
                            int(v.running), v.path, int(v.assembled), v.worst_run, good, bad])
    return path


def crossing_check(feed: M.Feed) -> None:
    """The claim the sweep exists to support."""
    tracks = {t.name: t for t in feed.tracks}
    if "dratsn" not in tracks or "vanilla" not in tracks:
        return
    worst = 0.0
    for sigma in SIGMAS:
        for threshold in THRESHOLDS:
            for med in MEDIANS:
                d = M.expected_summary(tracks["dratsn"], med, threshold, sigma=sigma)["defect_rate"]
                v = M.expected_summary(tracks["vanilla"], med, threshold, sigma=sigma)["defect_rate"]
                worst = max(worst, d - v)
    print(f"max(dratsn - vanilla) defect rate over the whole sweep: {worst:+.6f}")
    assert worst <= 0.0, "the tracks cross somewhere in the sweep"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", help="scenario feed json (default: built-in scenario A)")
    ap.add_argument("--out", default="results/dev", help="output directory")
    ap.add_argument("--lambda", dest="lam", type=float, default=0.5,
                    help="unprotected-path lognormal median (ms) for cycles.csv and the rendered run")
    ap.add_argument("--sigma", type=float, default=M.SIGMA,
                    help="unprotected-path lognormal shape for cycles.csv")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    feed = M.load_feed(args.feed) if args.feed else M.default_fault_feed()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"scenario={feed.scenario} tracks={[t.name for t in feed.tracks]}")
    print(f"{M.n_control_cycles()} control cycles per dwell, {M.N_CYCLES} production cycles")

    sweep = write_sweep(feed, out_dir)
    cycles = write_cycles(feed, out_dir, args.lam, args.sigma, args.seed)
    crossing_check(feed)

    print(f"wrote {sweep}")
    print(f"wrote {cycles}  (median={args.lam}ms sigma={args.sigma} seed={args.seed})")
    for track in feed.tracks:
        s = M.expected_summary(track, args.lam)
        print(f"  {track.name:9s} produced={s['produced']:3d} stopped={s['stopped']:3d} "
              f"defect_rate={s['defect_rate']:.3f}")


if __name__ == "__main__":
    main()
