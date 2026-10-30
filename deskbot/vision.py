"""The bot's eyes: webcam and screen frames, kept in memory only (never written to disk)."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

import numpy as np

from .config import Settings

log = logging.getLogger(__name__)

try:
    import cv2
    CV_ERROR = ""
except Exception as e:
    cv2 = None
    CV_ERROR = f"OpenCV unavailable: {e}"


def encode_jpeg(frame: np.ndarray, max_width: int, quality: int) -> bytes:
    h, w = frame.shape[:2]
    if w > max_width:
        frame = cv2.resize(frame, (max_width, int(h * max_width / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    return buf.tobytes() if ok else b""


def small_gray(jpeg: bytes) -> np.ndarray | None:
    if not jpeg or cv2 is None:
        return None
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_4)
    if img is None:
        return None
    return cv2.resize(img, (96, 54), interpolation=cv2.INTER_AREA).astype(np.float32)


def change_score(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 1.0
    return float(np.mean(np.abs(a - b)) / 255.0)


def composite(screen: bytes | None, cam: bytes | None, max_width: int, quality: int) -> bytes | None:
    """One frame for Live mode: the screen with the webcam as a picture-in-picture."""
    if not screen or not cam or cv2 is None:
        return screen or cam
    s = cv2.imdecode(np.frombuffer(screen, np.uint8), cv2.IMREAD_COLOR)
    c = cv2.imdecode(np.frombuffer(cam, np.uint8), cv2.IMREAD_COLOR)
    if s is None or c is None:
        return screen or cam
    h, w = s.shape[:2]
    cw = w // 4
    ch = int(c.shape[0] * cw / c.shape[1])
    c = cv2.resize(c, (cw, ch))
    s[h - ch - 8:h - 8, w - cw - 8:w - 8] = c
    cv2.rectangle(s, (w - cw - 9, h - ch - 9), (w - 8, h - 8), (40, 40, 230), 2)
    return encode_jpeg(s, max_width, quality)


class FrameSlot:
    def __init__(self):
        self._lock = threading.Lock()
        self.jpeg: bytes | None = None
        self.ts = 0.0

    def put(self, jpeg: bytes) -> None:
        with self._lock:
            self.jpeg = jpeg
            self.ts = time.monotonic()

    def get(self, max_age: float = 30.0) -> bytes | None:
        with self._lock:
            if self.jpeg and time.monotonic() - self.ts <= max_age:
                return self.jpeg
            return None

    def clear(self) -> None:
        with self._lock:
            self.jpeg = None


class Camera:
    def __init__(self, on_error: Callable[[str], None]):
        self.slot = FrameSlot()
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, index: int, max_width: int, quality: int) -> None:
        if cv2 is None:
            raise RuntimeError(CV_ERROR)
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(index, max_width, quality),
                                        daemon=True, name="camera")
        self._thread.start()

    def _run(self, index: int, max_width: int, quality: int) -> None:
        backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else (
            cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY)
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            self.on_error(f"Can't open camera {index}. Try another index in Settings → Eyes.")
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        fails = 0
        last = 0.0
        while not self._stop.is_set():
            ok, frame = cap.read()
            if not ok:
                fails += 1
                if fails > 50:
                    self.on_error("Camera stopped sending pictures.")
                    break
                time.sleep(0.05)
                continue
            fails = 0
            now = time.monotonic()
            if now - last >= 0.25:  # read continuously so the frame is fresh; encode 4x/s
                last = now
                self.slot.put(encode_jpeg(frame, max_width, quality))
        cap.release()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self.slot.clear()


def detect_cameras(max_index: int = 6) -> list[int]:
    if cv2 is None:
        return []
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i, cv2.CAP_V4L2) if sys.platform.startswith("linux") else cv2.VideoCapture(i)
        if cap.isOpened():
            ok, _ = cap.read()
            if ok:
                found.append(i)
        cap.release()
    return found


def session_type() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    return "x11"


class Screen:
    """Screen frames from the portal (Wayland), mss (X11/Windows) or a custom command."""

    def __init__(self, cfg: Settings, on_error: Callable[[str], None]):
        self.cfg = cfg
        self.slot = FrameSlot()
        self.on_error = on_error
        self.portal = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def backend(self) -> str:
        b = self.cfg.get("eyes.screen_backend", "auto")
        if b != "auto":
            return b
        # Under XWayland, X11 grabs only see X11 windows, so the real session type decides.
        return "portal" if session_type() == "wayland" else "mss"

    def start(self) -> None:
        self.stop()
        self._stop.clear()
        b = self.backend()
        g = self.cfg.get
        if b == "portal":
            from .portal import PortalCapture
            self.portal = PortalCapture(self.slot.put, self._portal_error,
                                        lambda tok: self.cfg.set("eyes.restore_token", tok))
            # the portal dialog can block for a while: do it off the UI thread
            def go():
                try:
                    self.portal.start(g("eyes.restore_token", ""), g("eyes.gst_template"),
                                      g("eyes.screen_max_width"), g("eyes.screen_fps"), g("eyes.jpeg_quality"))
                except Exception as e:
                    log.exception("portal start failed")
                    self.on_error(str(e))
            threading.Thread(target=go, daemon=True, name="portal-start").start()
        else:
            self._thread = threading.Thread(target=self._poll, args=(b,), daemon=True, name="screen")
            self._thread.start()

    def _portal_error(self, msg: str) -> None:
        if not self._stop.is_set():
            self.on_error(msg)

    def _poll(self, backend: str) -> None:
        g = self.cfg.get
        try:
            if backend == "mss":
                import mss
                with mss.mss() as sct:
                    while not self._stop.is_set():
                        mons = sct.monitors
                        idx = min(max(1, int(g("eyes.screen_monitor", 1))), len(mons) - 1)
                        shot = np.asarray(sct.grab(mons[idx]))[:, :, :3]
                        self.slot.put(encode_jpeg(np.ascontiguousarray(shot),
                                                  g("eyes.screen_max_width"), g("eyes.jpeg_quality")))
                        self._stop.wait(1.0 / max(0.2, float(g("eyes.screen_fps", 1.0))))
            elif backend == "command":
                out = os.path.join(tempfile.gettempdir(), f"deskbot-shot-{os.getpid()}.png")
                try:
                    while not self._stop.is_set():
                        cmd = g("eyes.screen_command").replace("{out}", out)
                        subprocess.run(cmd, shell=True, timeout=10, capture_output=True)
                        if os.path.exists(out):
                            img = cv2.imread(out, cv2.IMREAD_COLOR)
                            os.remove(out)
                            if img is not None:
                                self.slot.put(encode_jpeg(img, g("eyes.screen_max_width"),
                                                          g("eyes.jpeg_quality")))
                        self._stop.wait(max(1.0, 1.0 / max(0.2, float(g("eyes.screen_fps", 1.0)))))
                finally:
                    if os.path.exists(out):
                        os.remove(out)
        except Exception as e:
            log.exception("screen capture failed")
            self.on_error(f"Screen capture failed: {e}")

    def stop(self) -> None:
        self._stop.set()
        if self.portal:
            self.portal.stop()
            self.portal = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        self.slot.clear()
