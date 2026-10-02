"""HTTP poll thread. stdlib only — never imports omni or pxr; the update
callback on the Kit main thread is the only place USD is touched."""
import json
import threading
import time
import urllib.request


class Poller(threading.Thread):
    def __init__(self, url, hz=2.0, timeout=1.0):
        super().__init__(daemon=True)
        self.url = url
        self.period = 1.0 / max(hz, 0.1)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._snap = None
        self._stop = threading.Event()
        self._backoff = self.period
        self.error = None            # current error string, None when healthy

    def run(self):
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=self.timeout) as r:
                    snap = json.loads(r.read().decode())
                with self._lock:
                    self._snap = snap
                if self.error:
                    print(f"[dratsn.live] server back: {self.url}")
                self.error = None
                self._backoff = self.period
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                if msg != self.error:      # log state changes only
                    print(f"[dratsn.live] poll failed ({msg}), keeping last snapshot")
                self.error = msg
                self._backoff = min(self._backoff * 2, 5.0)
            self._stop.wait(self._backoff)

    def latest(self):
        with self._lock:
            return self._snap

    def stop(self):
        self._stop.set()
