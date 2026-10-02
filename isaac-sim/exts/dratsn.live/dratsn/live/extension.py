"""dratsn.live — Kit extension: live cluster overlay (ADR-0031 live mode).

Polls the fig_regime live_serve.py endpoint in a background thread and
rewrites the v11 scene's /World/ClusterOverlay prim attributes in the
session layer on the Kit main thread. The baked replay path is untouched:
disable the extension (or stop the server) and the scene is exactly the
baked file again.
"""
import os
import sys
import time

import carb.settings
import omni.ext
import omni.kit.app
import omni.timeline
import omni.usd

SETTINGS_ROOT = "/exts/dratsn.live/"


class DratsnLiveExtension(omni.ext.IExt):
    def on_startup(self, ext_id):
        s = carb.settings.get_settings()
        # urls: comma-separated — one server per arm/cluster for the dual live
        # demo (DRATSN + VanillaK8s zones moving at once). `url` kept as the
        # single-server fallback.
        urls = s.get(SETTINGS_ROOT + "urls") or \
            s.get(SETTINGS_ROOT + "url") or "http://127.0.0.1:8787/state"
        urls = [u.strip() for u in urls.split(",") if u.strip()]
        poll_hz = s.get(SETTINGS_ROOT + "poll_hz") or 2.0
        scripts_dir = s.get(SETTINGS_ROOT + "scripts_dir") or \
            "/isaac-sim/Documents/dratsn_factory/scripts"
        auto_stage = s.get(SETTINGS_ROOT + "auto_stage") or ""

        if scripts_dir not in sys.path:      # sim_model for the gauge SIGMA
            sys.path.insert(0, scripts_dir)
        from . import applier, floor_map, overlay_map, poller
        self._applier = applier
        self._map = overlay_map
        self._floor_map = floor_map
        self._floors = {}        # track -> FloorState

        self._arms = {}          # arm -> {"anim", "seq", "t0"}
        self._cleared = False    # session cleared once, before the first paint
        self._warned_missing = False
        self._auto_stage = auto_stage
        self._open_attempt = 0.0
        self._pollers = [poller.Poller(u, hz=poll_hz) for u in urls]
        for p in self._pollers:
            p.start()
        self._sub = omni.kit.app.get_app().get_update_event_stream() \
            .create_subscription_to_pop(self._on_update, name="dratsn.live.update")
        print(f"[dratsn.live] started, polling {urls} at {poll_hz} Hz")

    def _on_update(self, _e):
        try:
            self._tick()
        except Exception as e:               # never kill the frame loop
            print(f"[dratsn.live] update error: {type(e).__name__}: {e}")

    def _tick(self):
        ctx = omni.usd.get_context()
        stage = ctx.get_stage()
        if stage is None or not stage.GetPrimAtPath("/World/ClusterOverlay"):
            # the app boots into an empty stage after our startup ran, so the
            # scene open has to be re-issued from the update loop. Open ONLY
            # if the context is not already on our stage — a second open while
            # the (slow) first one loads replaces the stage object and silently
            # discards every session-layer opinion already painted.
            now = time.monotonic()
            url = ctx.get_stage_url() or ""
            if self._auto_stage and not url.endswith(
                    self._auto_stage.rsplit("/", 1)[-1]) \
                    and now - self._open_attempt > 30.0:
                self._open_attempt = now
                print(f"[dratsn.live] opening stage {self._auto_stage}")
                ctx.open_stage(self._auto_stage)
            return
        # a replaced stage (reopen, hand-open) starts with an empty session
        # layer: drop the diff caches so everything repaints, and stop the
        # timeline — live mode owns the overlay, the baked replay must not
        # run underneath it (ADR-0033: replay and live are separate paths)
        sid = ctx.get_stage_id()
        if sid != getattr(self, "_stage_id", None):
            self._stage_id = sid
            self._arms = {}
            self._floors = {}
            self._cleared = False
            self._tl_started = False
            self._replay_warned = False
            try:
                tl = omni.timeline.get_timeline_interface()
                tl.stop()
                tl.set_current_time(0.0)
            except Exception as e:
                print(f"[dratsn.live] timeline stop failed: {e}")
            print("[dratsn.live] stage (re)detected — full repaint, timeline stopped")
        # hybrid mode gate: live drives ONLY the live root (subLayers
        # [overlay, base_live]); a hand-opened replay scene is left alone so
        # the baked path stays watchable.
        root_id = stage.GetRootLayer().identifier
        self._live_root = "_live" in root_id.rsplit("/", 1)[-1]
        if not self._live_root:
            if not getattr(self, "_replay_warned", False):
                self._replay_warned = True
                print(f"[dratsn.live] {root_id} is not the live root — "
                      "standing down (baked replay mode)")
            return
        # timeline policy: the base_live rhythm loop must PLAY. Re-play on
        # user pause, loop at the 2880-frame seam (bar phase 120%5==0).
        try:
            tl = omni.timeline.get_timeline_interface()
            tps = stage.GetTimeCodesPerSecond() or 24.0
            frame = tl.get_current_time() * tps
            if frame >= self._floor_map.LIVE_LOOP_FRAMES - 1:
                tl.set_current_time(0.0)
                frame = 0.0
            if not tl.is_playing():
                tl.play()
                if not getattr(self, "_tl_started", False):
                    self._tl_started = True
                    print("[dratsn.live] rhythm loop playing (base_live)")
            self._frame = frame
        except Exception as e:
            print(f"[dratsn.live] timeline error: {e}")
            self._frame = 0.0
        # a restarted app boots with the default perspective camera (an unlit
        # angle showing only emissives — reads as a black screen with neon
        # blobs); pin the demo camera once per stage
        if not getattr(self, "_cam_set", False) or \
                self._cam_set != self._stage_id:
            try:
                from omni.kit.viewport.utility import get_active_viewport
                cam = carb.settings.get_settings().get(
                    SETTINGS_ROOT + "camera") or "/World/Cameras/Overview"
                get_active_viewport().camera_path = cam
                self._cam_set = self._stage_id
                print(f"[dratsn.live] camera -> {cam}")
            except Exception as e:
                print(f"[dratsn.live] camera set failed: {e}")
        now = time.monotonic()
        ops = []
        for p in self._pollers:
            combined = p.latest()
            if not combined:
                continue
            # v3 combined snapshot {arms:{...}, plc:{...}} or a legacy v2
            # single-arm snapshot — normalize to a list of arm snapshots
            if combined.get("v") == 3:
                lam = combined.get("lam_ms", 0.5)
                arm_snaps = []
                for arm, s in (combined.get("arms") or {}).items():
                    s = dict(s)
                    s.setdefault("lam_ms", lam)
                    s["seq"] = combined["seq"]
                    arm_snaps.append(s)
                plc = combined.get("plc") or {}
            else:
                arm_snaps = [combined]
                plc = {}
            for snap in arm_snaps:
                ops.extend(self._arm_tick(stage, snap, now))
            ops.extend(self._floor_tick(arm_snaps, plc, now,
                                        lam=combined.get("lam_ms", 0.5)))
        if ops:
            missing = self._applier.apply(stage, ops)
            if missing and not self._warned_missing:
                self._warned_missing = True
                print(f"[dratsn.live] missing prims (first occurrence): {missing[:5]}")
        self._maybe_capture(now)

    def _floor_tick(self, arm_snaps, plc, now, lam):
        """live floor: each track's cell runs while its controller runs
        (dratsn/vanilla = tracked vPLC state, physical = PLC state machine).
        The timeline owns the rhythm; on a running edge the cell's rhythm
        attrs are frozen at the current frame / released back."""
        ops = []
        frame = getattr(self, "_frame", 0.0)
        by_arm = {s.get("arm"): s for s in arm_snaps}
        tracks = [("physical", None)] + [(a, by_arm.get(a))
                                         for a in ("vanilla", "dratsn")]
        for track, snap in tracks:
            fs = self._floors.get(track)
            if fs is None:
                fs = self._floors[track] = self._floor_map.FloorState(
                    track, lam_ms=lam)
            if track == "physical":
                running = (plc.get("state") or "running") == "running"
                on_tsn = tagged = True       # declared: wired PLC = protected
            else:
                if snap is None:
                    continue
                tr = snap.get("tracked") or {}
                running = tr.get("state") in ("running", "running_again")
                on_tsn = bool(tr.get("on_tsn"))
                tagged = bool(tr.get("tagged"))
            edge = (fs.running_prev is None and not running) or \
                (fs.running_prev is not None and running != fs.running_prev)
            if edge:
                kind = "clear" if running else "freeze"
                for path, attrs in self._floor_map.freeze_targets(track):
                    if kind == "freeze":
                        ops.append(("freeze", path, attrs, frame))
                    else:
                        ops.append(("clear", path, attrs))
                print(f"[dratsn.live] floor {track}: {kind} rhythm at f{frame:.0f}")
            fs.running_prev = running
            try:
                ops.extend(fs.tick(frame, running, on_tsn, tagged))
            except Exception as e:
                print(f"[dratsn.live] floor {track}: {type(e).__name__}: {e}")
        return ops

    def _arm_tick(self, stage, snap, now):
        ops = []
        if True:
            arm = snap["arm"]
            st = self._arms.setdefault(
                arm, {"anim": self._map.AnimState(), "seq": None, "t0": None})
            anim = st["anim"]
            if not anim.alias:
                zone = self._map.ZONE_OF_ARM.get(arm)
                live = [n["name"] for n in snap["cluster"]["nodes"]]
                baked = self._applier.baked_node_leaves(stage, zone) if zone else []
                if len(baked) == len(live):
                    anim.alias = dict(zip(live, baked))
                    if any(anim.alias[k] != k.replace("-", "_") for k in anim.alias):
                        print(f"[dratsn.live] {arm}: node alias {anim.alias}")
            if snap.get("seq") != st["seq"]:
                if snap.get("t0_ms") != st["t0"]:    # new inject-fault
                    anim.reset_fault()
                    st["t0"] = snap.get("t0_ms")
                first = st["seq"] is None
                if first and not self._cleared:      # stale dev-loop opinions
                    self._applier.clear_session(stage)
                    self._cleared = True
                static_ops, moves, pulses = self._map.compute_ops(
                    snap, anim, first=first)
                anim.add_moves(moves, now)
                anim.add_pulses(pulses, now)
                ops.extend(static_ops)
                st["seq"] = snap.get("seq")
                if first:
                    print(f"[dratsn.live] first snapshot painted: arm={arm} "
                          f"phase={snap.get('phase')} ops={len(static_ops)}")
                elif static_ops or moves or pulses:
                    print(f"[dratsn.live] arm={arm} seq={snap.get('seq')} "
                          f"phase={snap.get('phase')} ops={len(static_ops)} "
                          f"moves={len(moves)} pulses={len(pulses)}")
            ops.extend(anim.tick(now))
        return ops

    _CAP_DIR = "/isaac-sim/Documents/dratsn_factory"

    def _maybe_capture(self, now):
        """Drop a file at <Documents>/dratsn_factory/capture.req (host side:
        documents/dratsn_factory/) and the extension saves the current
        viewport to preview/live_capture.png — remote debugging + demo
        stills without touching the stream."""
        pend = getattr(self, "_cap_pending", None)
        if pend is not None:             # phase 2: camera has had a second
            if now < pend:               # of updates — capture now
                return
            try:
                from omni.kit.viewport.utility import (capture_viewport_to_file,
                                                       get_active_viewport)
                out = f"{self._CAP_DIR}/preview/live_capture.png"
                capture_viewport_to_file(get_active_viewport(), out)
                print(f"[dratsn.live] viewport captured -> {out}")
            except Exception as e:
                print(f"[dratsn.live] capture failed: {type(e).__name__}: {e}")
            self._cap_pending = None
            return
        if now < getattr(self, "_cap_next", 0.0):
            return
        self._cap_next = now + 2.0
        req = f"{self._CAP_DIR}/capture.req"
        if not os.path.exists(req):
            return
        try:
            from omni.kit.viewport.utility import (capture_viewport_to_file,
                                                   get_active_viewport)
            with open(req) as f:
                cam = f.read().strip()   # optional camera prim path
            os.remove(req)
            if cam:                      # phase 1: switch, capture next tick
                get_active_viewport().camera_path = cam
                self._cap_pending = now + 1.0
                print(f"[dratsn.live] camera -> {cam}, capturing shortly")
            else:
                out = f"{self._CAP_DIR}/preview/live_capture.png"
                capture_viewport_to_file(get_active_viewport(), out)
                print(f"[dratsn.live] viewport captured -> {out}")
        except Exception as e:
            self._cap_pending = None
            print(f"[dratsn.live] capture failed: {type(e).__name__}: {e}")

    def on_shutdown(self):
        self._sub = None
        for p in getattr(self, "_pollers", []):
            p.stop()
        for p in getattr(self, "_pollers", []):
            p.join(2.0)
        stage = omni.usd.get_context().get_stage()
        if stage is not None:
            try:
                self._applier.clear_session(stage)
            except Exception:
                pass
        print("[dratsn.live] stopped")
