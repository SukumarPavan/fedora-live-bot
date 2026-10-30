"""Microphone capture and a stoppable player that reports loudness for lip sync."""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

MIC_SR = 16000
MIC_BLOCK = 512          # 32 ms at 16 kHz, the Silero VAD frame size
OUT_SR = 24000

try:
    import sounddevice as sd
    AUDIO_ERROR = ""
except Exception as e:  # PortAudio missing
    sd = None
    AUDIO_ERROR = f"Audio unavailable: {e}. On Fedora run: sudo dnf install portaudio"


def list_devices(kind: str) -> list[str]:
    """Names of input ('input') or output ('output') devices."""
    if sd is None:
        return []
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        return [d["name"] for d in sd.query_devices() if d[key] > 0]
    except Exception:
        return []


def _device_index(name: str, kind: str) -> int | None:
    if not name or sd is None:
        return None
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and d[key] > 0:
            return i
    log.warning("Audio device %r not found, using default", name)
    return None


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(x) == 0:
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(x.dtype)


class Mic:
    """Delivers int16 mono blocks of MIC_BLOCK samples at 16 kHz to `on_block`."""

    def __init__(self, on_block: Callable[[np.ndarray], None]):
        self.on_block = on_block
        self.stream = None
        self._rate = MIC_SR
        self._buf = np.zeros(0, dtype=np.int16)

    def start(self, device_name: str = "") -> None:
        if sd is None:
            raise RuntimeError(AUDIO_ERROR)
        self.stop()
        dev = _device_index(device_name, "input")
        for rate in (MIC_SR, 48000, 44100):
            try:
                self.stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                             blocksize=int(MIC_BLOCK * rate / MIC_SR),
                                             device=dev, callback=self._cb)
                self._rate = rate
                self.stream.start()
                log.info("Mic started at %d Hz (device=%s)", rate, device_name or "default")
                return
            except Exception as e:
                log.warning("Mic open at %d Hz failed: %s", rate, e)
                self.stream = None
        raise RuntimeError("Could not open the microphone")

    def _cb(self, indata, frames, time_info, status) -> None:
        x = indata[:, 0].copy()
        if self._rate != MIC_SR:
            x = resample(x.astype(np.float32), self._rate, MIC_SR).astype(np.int16)
        self._buf = np.concatenate([self._buf, x])
        while len(self._buf) >= MIC_BLOCK:
            block, self._buf = self._buf[:MIC_BLOCK], self._buf[MIC_BLOCK:]
            try:
                self.on_block(block)
            except Exception:
                log.exception("mic consumer failed")

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


def robotize(x: np.ndarray, amount: float, sr: int = OUT_SR) -> np.ndarray:
    """Light metallic voice: short comb filter plus a little ring modulation."""
    if amount <= 0.01 or len(x) == 0:
        return x
    d = int(sr * 0.0045)
    comb = x.copy()
    comb[d:] += 0.55 * amount * x[:-d]
    t = np.arange(len(x), dtype=np.float32) / sr
    ring = comb * np.sin(2 * np.pi * 55.0 * t)
    y = (1 - 0.35 * amount) * comb + 0.35 * amount * ring
    peak = float(np.max(np.abs(y))) or 1.0
    return (y / max(1.0, peak)).astype(np.float32)


class Player:
    """Float32 mono queue player at 24 kHz. `clear()` stops speech instantly."""

    def __init__(self):
        self.stream = None
        self._q: collections.deque[np.ndarray] = collections.deque()
        self._cur = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._rate = OUT_SR
        self.gain = 1.0
        self.level = 0.0          # smoothed loudness 0..1 for lip sync
        self.out_rms = 0.0        # raw loudness of what is being played (echo estimation)
        self.last_audio_t = 0.0   # monotonic time the last non-silent block was played
        self.active = False       # True while audio is queued or playing
        self.idle_event = threading.Event()
        self.idle_event.set()

    def start(self, device_name: str = "") -> None:
        if sd is None:
            raise RuntimeError(AUDIO_ERROR)
        self.stop()
        dev = _device_index(device_name, "output")
        for rate in (OUT_SR, 48000, 44100):
            try:
                self.stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32",
                                              blocksize=int(rate * 0.02), device=dev,
                                              callback=self._cb)
                self._rate = rate
                self.stream.start()
                log.info("Speaker started at %d Hz (device=%s)", rate, device_name or "default")
                return
            except Exception as e:
                log.warning("Speaker open at %d Hz failed: %s", rate, e)
                self.stream = None
        raise RuntimeError("Could not open the speaker")

    def play(self, pcm: np.ndarray) -> None:
        if len(pcm) == 0:
            return
        if self.stream is None:  # no speaker: never mark as playing, or engines wait forever
            log.warning("No speaker open; dropping %.1f s of speech", len(pcm) / OUT_SR)
            return
        if self._rate != OUT_SR:
            pcm = resample(pcm, OUT_SR, self._rate)
        with self._lock:
            self._q.append(pcm.astype(np.float32))
            self.active = True
            self.idle_event.clear()

    def clear(self) -> None:
        with self._lock:
            self._q.clear()
            self._cur = np.zeros(0, dtype=np.float32)
            self.active = False
            self.level = 0.0
            self.idle_event.set()

    def _cb(self, outdata, frames, time_info, status) -> None:
        out = np.zeros(frames, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < frames:
                if len(self._cur) == 0:
                    if not self._q:
                        break
                    self._cur = self._q.popleft()
                n = min(frames - filled, len(self._cur))
                out[filled:filled + n] = self._cur[:n]
                self._cur = self._cur[n:]
                filled += n
            if filled == 0 and self.active and not self._q and len(self._cur) == 0:
                self.active = False
                self.idle_event.set()
        out *= self.gain
        rms = float(np.sqrt(np.mean(out * out))) if filled else 0.0
        self.out_rms = rms
        if filled:
            self.last_audio_t = time.monotonic()
        target = min(1.0, rms * 6.0)
        self.level += (target - self.level) * (0.6 if target > self.level else 0.25)
        outdata[:, 0] = np.clip(out, -1.0, 1.0)

    def stop(self) -> None:
        self.clear()
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


def pcm16_to_float(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0


def wav_bytes(pcm16: np.ndarray, sr: int = MIC_SR) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.astype(np.int16).tobytes())
    return buf.getvalue()
