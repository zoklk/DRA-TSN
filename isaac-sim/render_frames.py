#!/usr/bin/env python3
"""Render still frames from a baked scenario stage.

Used to check the animation layer without sitting in front of the WebRTC stream,
and to produce the paper stills. Runs inside the Isaac Sim container:

  ./python.sh render_frames.py scene.usda --frames 200,960,1200,2400 --out preview/
"""

from __future__ import annotations

import argparse
from pathlib import Path

_ap = argparse.ArgumentParser()
_ap.add_argument("stage")
_ap.add_argument("--frames", default="200,960,1200,2400",
                 help="comma-separated frame numbers")
_ap.add_argument("--range", dest="rng",
                 help="START-END[:STEP] to render a whole sequence for video")
_ap.add_argument("--camera", default="/World/Cameras/Overview")
_ap.add_argument("--out", default="preview")
_ap.add_argument("--fps", type=float, default=24.0)
_ap.add_argument("--settle", type=int, default=40, help="app updates per frame before capture")
ARGS, _ = _ap.parse_known_args()

from isaacsim import SimulationApp

simulation_app = SimulationApp({"headless": True, "width": 1280, "height": 720})

import omni.timeline
import omni.usd
from omni.kit.viewport.utility import capture_viewport_to_file, get_active_viewport


def main() -> None:
    out_dir = Path(ARGS.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx = omni.usd.get_context()
    ctx.open_stage(ARGS.stage)
    for _ in range(60):
        simulation_app.update()

    viewport = get_active_viewport()
    viewport.camera_path = ARGS.camera
    timeline = omni.timeline.get_timeline_interface()
    timeline.stop()

    if ARGS.rng:
        span, _, step = ARGS.rng.partition(":")
        lo, _, hi = span.partition("-")
        frames = range(int(lo), int(hi) + 1, int(step or 1))
    else:
        frames = [int(f) for f in ARGS.frames.split(",")]

    for frame in frames:
        timeline.set_current_time(frame / ARGS.fps)
        for _ in range(ARGS.settle):
            simulation_app.update()
        path = out_dir / f"frame_{frame:05d}.png"
        capture_viewport_to_file(viewport, str(path))
        for _ in range(ARGS.settle):
            simulation_app.update()
        if not ARGS.rng or frame % 240 == 0:
            print(f"captured {path}", flush=True)


if __name__ == "__main__":
    main()
    simulation_app.close()
