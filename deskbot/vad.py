"""Voice activity detection (Silero ONNX, energy fallback) and utterance endpointing."""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass

import numpy as np

from .audio_io import MIC_BLOCK, MIC_SR
from .config import VAD_MODEL

log = logging.getLogger(__name__)


class SileroVAD:
    CONTEXT = 64  # Silero v5 expects the last 64 samples of the previous frame prepended

    def __init__(self, path=VAD_MODEL):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.sess = ort.InferenceSession(str(path), sess_options=opts,
                                         providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def __call__(self, block: np.ndarray) -> float:
        x = (block.astype(np.float32) / 32768.0).reshape(1, -1)
        x = np.concatenate([self.context, x], axis=1)
        out, self.state = self.sess.run(None, {"input": x, "state": self.state,
                                               "sr": np.array(MIC_SR, dtype=np.int64)})
        self.context = x[:, -self.CONTEXT:]
        return float(out[0][0])


class EnergyVAD:
    """Fallback when onnxruntime is unavailable: adaptive noise floor."""

    def __init__(self):
        self.floor = 300.0

    def reset(self) -> None:
        pass

    def __call__(self, block: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
        self.floor = 0.995 * self.floor + 0.005 * min(rms, self.floor * 2)
        return float(np.clip((rms / max(self.floor, 50.0) - 1.5) / 3.0, 0.0, 1.0))


def make_vad():
    try:
        return SileroVAD()
    except Exception as e:
        log.warning("Silero VAD unavailable (%s); using energy VAD", e)
        return EnergyVAD()


@dataclass
class VadParams:
    threshold: float = 0.5
    silence_ms: int = 700
    min_speech_ms: int = 250
    barge_threshold: float = 0.85
    barge_ms: int = 300
    max_utterance_s: float = 30.0
    preroll_ms: int = 320


class Endpointer:
    """Turns per-block speech probabilities into speech-start and utterance events.

    feed() returns a list of events: ("start", None) and ("utterance", int16 array)."""

    def __init__(self, vad, params: VadParams):
        self.vad = vad
        self.p = params
        self.block_ms = MIC_BLOCK * 1000 / MIC_SR
        self.reset()

    def reset(self) -> None:
        self.in_speech = False
        self.run = 0          # consecutive blocks above the start threshold
        self.quiet = 0        # consecutive blocks below the end threshold
        self.voiced = 0       # voiced blocks in the current utterance
        self.frames: list[np.ndarray] = []
        self.pre: collections.deque[np.ndarray] = collections.deque(
            maxlen=max(1, int(self.p.preroll_ms / self.block_ms)))
        self.last_prob = 0.0

    def feed(self, block: np.ndarray, bot_speaking: bool = False, gate=None) -> list[tuple[str, object]]:
        """gate(block, prob, threshold) -> bool can veto speech starts while the bot is audible."""
        prob = self.vad(block)
        self.last_prob = prob
        events: list[tuple[str, object]] = []
        p = self.p
        start_thr = p.barge_threshold if bot_speaking else p.threshold
        start_blocks = max(1, int((p.barge_ms if bot_speaking else 96) / self.block_ms))

        if not self.in_speech:
            self.pre.append(block)
            voiced = prob >= start_thr
            if bot_speaking and gate is not None:
                voiced = gate(block, prob, start_thr)
            self.run = self.run + 1 if voiced else 0
            if self.run >= start_blocks:
                self.in_speech = True
                self.frames = list(self.pre)
                self.voiced = self.run
                self.quiet = 0
                events.append(("start", None))
            return events

        self.frames.append(block)
        if prob >= p.threshold - 0.15:
            self.quiet = 0
            self.voiced += 1
        else:
            self.quiet += 1
        too_long = len(self.frames) * self.block_ms > p.max_utterance_s * 1000
        if self.quiet * self.block_ms >= p.silence_ms or too_long:
            audio = np.concatenate(self.frames)
            enough = self.voiced * self.block_ms >= p.min_speech_ms
            self.in_speech = False
            self.run = 0
            self.frames = []
            self.pre.clear()
            events.append(("utterance" if enough else "noise", audio))
        return events
