#!/usr/bin/env python3
"""Scenario decision model for the DRATSN twin.

Turns an injected placement feed into a per-cycle verdict (assembled / defective /
line stopped) for each control track. Deliberately free of any Isaac Sim or third
party dependency so it runs locally and is shared by two consumers:

  build_factory_scene.py   bakes the verdicts into USD time samples
  sweep.py                 sweeps lambda and writes metrics.csv

What is measured and what is declared (ADR-0036, 제어연동_시뮬레이션_설계.md):
  measured  which node a vPLC Pod lands on, how long the placement takes
  declared  the wire-delay distribution of an unprotected path: lognormal with
            median `lam_ms` and shape SIGMA (both swept). A protected path (P0)
            is not sampled at all — its delay is bounded by the 802.1Qbv
            max-latency the CNC reserves (QBV_BOUND_MS < T_CYCLE_MS), so it
            structurally cannot miss the deadline.
  geometry  the assembly dwell, which the scene constants fix

Lognormal, not exponential: measured packet delays on shared Ethernet fit a
lognormal (Fujimoto/Ata/Murata, IEICE Trans. Commun. 2001 — LAN delays are
lognormal and uncorrelated), and operational-network measurements put the
queueing tail of an unprotected path in the sub-ms..ms range under load
(Papagiannaki et al., INFOCOM 2004). The declared parameters take their order
of magnitude from there; the sweep defends the rest (ADR-0036).

A control loop runs periodically, not once per workpiece, so the verdict is taken
over the control cycles that fall inside one assembly dwell. Every control cycle
draws its own delay from the class distribution (per-cycle sampling, ADR-0036);
an isolated late sample is absorbed by the trajectory, a consecutive run of them
is not. A part is defective iff the longest run of missed deadlines reaches
RUN_THRESHOLD.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

# --- scene timing (shared with build_factory_scene.py) ---------------------
# The conveyor is indexed, not continuous: it advances one pitch, then holds at
# the work station while the robots act.
FPS = 24
MOVE_FRAMES = 10
DWELL_FRAMES = 14
CYCLE_FRAMES = MOVE_FRAMES + DWELL_FRAMES
COMPRESSION = 60  # one screen second is COMPRESSION real seconds
N_CYCLES = 120

# --- control model ---------------------------------------------------------
T_CYCLE_MS = 2.0  # vPLC scan cycle, real time — UR10e e-Series control loop (500 Hz)
RUN_THRESHOLD = 4  # consecutive missed deadlines that break the trajectory
SIGMA = 1.0  # declared lognormal shape of the unprotected wire delay (swept)
QBV_BOUND_MS = 1.0  # 802.1Qbv max-latency the CNC reserves per stream (MAX_LATENCY_NS)

CYCLE_REAL_MS = CYCLE_FRAMES / FPS * COMPRESSION * 1000.0
DWELL_REAL_MS = DWELL_FRAMES / FPS * COMPRESSION * 1000.0
MOVE_REAL_MS = MOVE_FRAMES / FPS * COMPRESSION * 1000.0

P0, P1, P2 = "P0", "P1", "P2"


def path_class(on_tsn: bool, tagged: bool) -> str:
    """Placement result -> path class.

    P0 is the only class the GCL protects, and reaching it needs both a correct
    placement and data plane enforcement. A vanilla track that happens to land on
    the TSN node still sits in P1 because nothing tags its frames.
    """
    if on_tsn and tagged:
        return P0
    return P1 if on_tsn else P2


def miss_rate(lam_ms: float, sigma: float = SIGMA) -> float:
    """P(command delay > one control cycle) for a lognormal delay.

    `lam_ms` is the distribution median (the parameter name survives from the
    exponential-scale era so the consumers keep working unchanged); `sigma` is
    the lognormal shape. Closed form via the lognormal CDF: ln(delay) is normal
    with mean ln(lam_ms) and stddev sigma.
    """
    if lam_ms <= 0.0:
        return 0.0
    return 0.5 * math.erfc((math.log(T_CYCLE_MS) - math.log(lam_ms)) / (sigma * math.sqrt(2.0)))


def sample_delay(cls: str, lam_ms: float, rng: random.Random, sigma: float = SIGMA) -> float:
    """One control command's wire delay (ms), drawn from the class distribution.

    P0 never exceeds the reserved Qbv bound; the shape inside the bound cannot
    affect a verdict, so uniform is enough (only the overlay gauge ever sees it).
    """
    if cls == P0:
        return rng.uniform(0.0, QBV_BOUND_MS)
    return rng.lognormvariate(math.log(lam_ms), sigma) if lam_ms > 0.0 else 0.0


def sample_part(q: float, n_control: int, threshold: int, rng: random.Random) -> tuple[bool, int]:
    """(assembled, worst run) for one part, sampling every control cycle.

    Each cycle independently misses its deadline with probability q — the
    lognormal tail from miss_rate(); drawing the miss indicator is equivalent
    to drawing the delay and comparing against T_CYCLE_MS, and 10x cheaper.
    Sampling continues after the verdict is decided so worst_run stays honest.
    """
    run = worst = 0
    for _ in range(n_control):
        if rng.random() < q:
            run += 1
            if run > worst:
                worst = run
        else:
            run = 0
    return worst < threshold, worst


def defect_prob(q: float, n_control: int, threshold: int = RUN_THRESHOLD) -> float:
    """P(a run of `threshold` consecutive misses occurs in n_control trials).

    Exact, via the run-length DP over states 0..threshold-1 plus an absorbing
    state. There is no closed form, which is the reason this model needs to be
    computed rather than asserted.
    """
    if q <= 0.0 or n_control <= 0:
        return 0.0
    if q >= 1.0:
        return 1.0 if n_control >= threshold else 0.0
    state = [0.0] * threshold
    state[0] = 1.0
    absorbed = 0.0
    for _ in range(n_control):
        alive = sum(state)
        nxt = [0.0] * threshold
        nxt[0] = alive * (1.0 - q)
        for k in range(threshold - 1):
            nxt[k + 1] = state[k] * q
        absorbed += state[threshold - 1] * q
        state = nxt
    # thousands of DP steps drift a few ulp past the unit interval
    return min(1.0, max(0.0, absorbed))


def n_control_cycles() -> int:
    """Control cycles that fall inside one assembly dwell."""
    return int(DWELL_REAL_MS / T_CYCLE_MS)


# --- feed ------------------------------------------------------------------


@dataclass
class Placement:
    node: str
    on_tsn: bool
    tagged: bool

    @property
    def cls(self) -> str:
        return path_class(self.on_tsn, self.tagged)


@dataclass
class Disruption:
    """One change event: the line goes down, then resumes at a new placement.

    The v2 feed fields (stages/evictions/peer, see fig_regime/build_feed.py —
    the schema SSOT) ride along untyped for the scene overlay; the verdict
    model itself never reads them.
    """

    t_ms: float
    down_ms: float
    resume: Placement
    label: str = ""
    stages: dict | None = None
    evictions: list[dict] | None = None
    peer: dict | None = None


@dataclass
class Track:
    name: str
    start: Placement
    disruptions: list[Disruption] = field(default_factory=list)


@dataclass
class Feed:
    scenario: str
    tracks: list[Track]
    compression: int = COMPRESSION
    cluster: dict | None = None  # v2 topology/ledger block, overlay-only


def _placement(d: dict) -> Placement:
    return Placement(str(d["node"]), bool(d["on_tsn"]), bool(d["tagged"]))


def load_feed(path: str | Path) -> Feed:
    raw = json.loads(Path(path).read_text())
    tracks = []
    for cell in raw["cells"]:
        tracks.append(
            Track(
                name=cell["track"],
                start=_placement(cell["start"]),
                disruptions=[
                    Disruption(
                        t_ms=float(d["t_ms"]),
                        down_ms=float(d["down_ms"]),
                        resume=_placement(d["resume"]),
                        label=d.get("label", ""),
                        stages=d.get("stages"),
                        evictions=d.get("evictions"),
                        peer=d.get("peer"),
                    )
                    for d in cell.get("disruptions", [])
                ],
            )
        )
    return Feed(raw.get("scenario", "fault"), tracks,
                int(raw.get("compression", COMPRESSION)), raw.get("cluster"))


# --- verdicts --------------------------------------------------------------


@dataclass
class CycleVerdict:
    cycle: int
    frame_start: int
    t_ms: float
    running: bool
    path: str
    assembled: bool
    worst_run: int = 0  # longest run of missed deadlines while this part was held


def state_at(track: Track, t_ms: float) -> tuple[bool, Placement]:
    """Line status and active placement at a real-time instant."""
    current = track.start
    for d in track.disruptions:
        if t_ms < d.t_ms:
            break
        if t_ms < d.t_ms + d.down_ms:
            return False, current
        current = d.resume
    return True, current


def run_track(track: Track, lam_ms: float, rng: random.Random, n_cycles: int = N_CYCLES,
              sigma: float = SIGMA) -> list[CycleVerdict]:
    """Per-cycle verdicts for one track, sampling every control cycle (ADR-0036).

    Each held part draws all of its control cycles from the class distribution,
    so the run structure (worst_run) is real, not just the final verdict. P0 is
    bounded below the deadline and skips the draw entirely.
    """
    n_control = n_control_cycles()
    q_cache: dict[str, float] = {}
    verdicts = []
    for i in range(n_cycles):
        frame_start = i * CYCLE_FRAMES
        # the verdict is taken while the part is held at the work station
        t_ms = i * CYCLE_REAL_MS + MOVE_REAL_MS
        running, placement = state_at(track, t_ms)
        if not running:
            verdicts.append(CycleVerdict(i, frame_start, t_ms, False, "-", False))
            continue
        cls = placement.cls
        if cls == P0:
            assembled, worst = True, 0
        else:
            if cls not in q_cache:
                q_cache[cls] = miss_rate(lam_ms, sigma)
            assembled, worst = sample_part(q_cache[cls], n_control, RUN_THRESHOLD, rng)
        verdicts.append(CycleVerdict(i, frame_start, t_ms, True, cls, assembled, worst))
    return verdicts


def run_feed(feed: Feed, lam_ms: float, seed: int = 0, n_cycles: int = N_CYCLES,
             sigma: float = SIGMA) -> dict[str, list[CycleVerdict]]:
    return {
        t.name: run_track(t, lam_ms, random.Random(seed + idx), n_cycles, sigma)
        for idx, t in enumerate(feed.tracks)
    }


def expected_summary(
    track: Track,
    lam_ms: float,
    threshold: int = RUN_THRESHOLD,
    n_cycles: int = N_CYCLES,
    sigma: float = SIGMA,
) -> dict[str, float]:
    """Same accounting as summarize() but analytic, with no sampling noise.

    Used by the sweep: the curve should show the model, not the seed. The
    per-cycle miss probability is the same lognormal tail run_track samples
    from, so the two agree in expectation (the self test checks this).
    """
    n_control = n_control_cycles()
    cache: dict[str, float] = {}
    produced = 0
    defect = 0.0
    for i in range(n_cycles):
        t_ms = i * CYCLE_REAL_MS + MOVE_REAL_MS
        running, placement = state_at(track, t_ms)
        if not running:
            continue
        produced += 1
        cls = placement.cls
        if cls not in cache:
            q = 0.0 if cls == P0 else miss_rate(lam_ms, sigma)
            cache[cls] = defect_prob(q, n_control, threshold)
        defect += cache[cls]
    return {
        "cycles": n_cycles,
        "produced": produced,
        "good": produced - defect,
        "defect": defect,
        "stopped": n_cycles - produced,
        "defect_rate": defect / produced if produced else 0.0,
    }


def summarize(verdicts: list[CycleVerdict]) -> dict[str, float]:
    produced = [v for v in verdicts if v.running]
    good = [v for v in produced if v.assembled]
    return {
        "cycles": len(verdicts),
        "produced": len(produced),
        "good": len(good),
        "defect": len(produced) - len(good),
        "stopped": len(verdicts) - len(produced),
        "defect_rate": (len(produced) - len(good)) / len(produced) if produced else 0.0,
    }


# --- default feed ----------------------------------------------------------


def default_fault_feed() -> Feed:
    """Scenario A with hand-made placements, for scene work before the collection
    experiment exists. Node names mirror the real cluster (alpha-w1 holds the TSN
    path); the vanilla track lands off it, which is what a blind scheduler does.
    """
    fail_at = 40 * CYCLE_REAL_MS
    return Feed(
        "fault",
        [
            Track(
                "physical",
                Placement("fixed-plc", True, True),
                [Disruption(fail_at, 3_600_000.0, Placement("fixed-plc", True, True), "PLC 교체")],
            ),
            # The vanilla track starts on the TSN node by luck. Nothing tags its
            # frames, so it sits in P1 and fails anyway; after the fault a blind
            # scheduler drops it onto a remote node (P2). Both halves of the
            # contribution argument are on screen in one run.
            Track(
                "vanilla",
                Placement("alpha-w1", True, False),
                [Disruption(fail_at, 640.0, Placement("alpha-w3", False, False), "재배치")],
            ),
            Track(
                "dratsn",
                Placement("alpha-w1", True, True),
                [Disruption(fail_at, 690.0, Placement("alpha-w1", True, True), "재배치")],
            ),
        ],
    )


def _self_test() -> None:
    n = n_control_cycles()
    print(f"dwell {DWELL_REAL_MS/1000:.1f}s real -> {n} control cycles of {T_CYCLE_MS}ms")
    print(f"cycle {CYCLE_REAL_MS/1000:.1f}s real, {N_CYCLES} cycles = {N_CYCLES*CYCLE_REAL_MS/3.6e6:.2f}h")

    assert path_class(True, True) == P0
    assert path_class(True, False) == P1
    assert path_class(False, False) == P2

    assert defect_prob(0.0, n) == 0.0
    assert defect_prob(1.0, n) == 1.0
    assert abs(defect_prob(0.5, 3, threshold=4)) < 1e-12, "run cannot fit in 3 trials"
    assert QBV_BOUND_MS < T_CYCLE_MS, "a Qbv-bounded path must fit inside the deadline"

    prev = -1.0
    for lam in (0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0, 50.0):
        p = defect_prob(miss_rate(lam), n)
        print(f"  median {lam:5.2f}ms  q={miss_rate(lam):.4f}  defect={p:.4f}")
        assert p >= prev - 1e-9, "defect probability must be monotone in the median"
        prev = p
    assert defect_prob(miss_rate(0.05), n) < 1e-6, "tiny median must be clean"
    assert defect_prob(miss_rate(1e6), n) > 0.999, "huge median must fail everything"

    # per-cycle sampling must agree with the analytic run-length DP
    rng = random.Random(7)
    q, n_small, parts = 0.2, 200, 4000
    p_exact = defect_prob(q, n_small)
    bad = sum(0 if sample_part(q, n_small, RUN_THRESHOLD, rng)[0] else 1 for _ in range(parts))
    p_emp = bad / parts
    tol = 4.0 * math.sqrt(p_exact * (1.0 - p_exact) / parts)
    print(f"sampling check: exact={p_exact:.4f} empirical={p_emp:.4f} tol={tol:.4f}")
    assert abs(p_emp - p_exact) <= tol, "sampled verdicts drifted off the analytic model"

    # v2 feed fields must survive load_feed and stay out of the verdict path
    import tempfile
    v2 = {
        "scenario": "fault", "compression": 60,
        "cluster": {"nodes": [{"name": "w1", "tsn": True,
                               "n": {"max_latency_ns": 1_000_000,
                                     "accumulated_ns": 250_000}}],
                    "fault_node": "w1",
                    "net": {"capacity_mbps": 1000, "by_arm": {}}},
        "cells": [{"track": "dratsn",
                   "start": {"node": "w1", "on_tsn": True, "tagged": True},
                   "disruptions": [{
                       "t_ms": 1000.0, "down_ms": 500.0,
                       "resume": {"node": "w2", "on_tsn": True, "tagged": True},
                       "stages": {"detect_ms": 400.0},
                       "evictions": [{"pod": "ai-x", "node": "w2",
                                      "priority_class": "it-ai",
                                      "ai_resched_ms": 450.0}],
                       "peer": {"vplc": "vplc-b",
                                "start": {"node": "w1", "on_tsn": True, "tagged": True},
                                "down_ms": 520.0,
                                "resume": {"node": "w3", "on_tsn": True, "tagged": True}},
                   }]}],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(v2, fh)
        v2_path = fh.name
    fv2 = load_feed(v2_path)
    d0 = fv2.tracks[0].disruptions[0]
    assert fv2.cluster["fault_node"] == "w1"
    assert d0.stages["detect_ms"] == 400.0
    assert d0.evictions[0]["pod"] == "ai-x"
    assert d0.peer["resume"]["node"] == "w3"
    assert run_feed(fv2, 0.5, seed=1), "v2 extras must not break the verdict path"
    print("v2 feed fields ok")

    feed = default_fault_feed()
    for lam in (0.1, 0.5):
        out = run_feed(feed, lam, seed=1)
        print(f"median={lam}ms")
        for name, v in out.items():
            s = summarize(v)
            worst = max((c.worst_run for c in v), default=0)
            print(f"  {name:9s} produced={s['produced']:3d} defect={s['defect']:3d} "
                  f"stopped={s['stopped']:3d} rate={s['defect_rate']:.3f} worst_run={worst}")
        assert summarize(out["dratsn"])["defect"] == 0, "P0 must never produce a defect"
        assert summarize(out["physical"])["stopped"] > 0, "PLC repair must stop the line"
    print("self test ok")


if __name__ == "__main__":
    _self_test()
