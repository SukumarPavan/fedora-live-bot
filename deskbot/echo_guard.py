"""Telling the user's voice apart from the bot's own voice leaking from speakers into the mic.

Without echo cancellation, a speech detector can't distinguish the two: the bot then
interrupts itself or answers its own words. While the bot is audible (and for a short
tail after), a block only counts as the user if speech is likely AND the mic is clearly
louder than the echo level learned from the bot's own playback."""
from __future__ import annotations

import re
import time

import numpy as np

TAIL_S = 0.8  # room echo and output latency after the last played sample


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) / 32768.0


class EchoGuard:
    def __init__(self, player, margin: float = 2.5):
        self.player = player
        self.margin = margin
        self.coupling = 0.6  # mic loudness / speaker loudness while only the bot talks

    def bot_audible(self) -> bool:
        return self.player.active or time.monotonic() - self.player.last_audio_t < TAIL_S

    def is_user(self, block: np.ndarray, prob: float, threshold: float) -> bool:
        mic = rms(block)
        out = self.player.out_rms
        if prob < threshold:
            if out > 0.003:  # learn how loud our own voice comes back
                self.coupling = 0.95 * self.coupling + 0.05 * min(4.0, mic / out)
            return False
        return mic > max(self.margin * self.coupling * out, 0.003)


_WORD = re.compile(r"\w+", re.U)


def sounds_like_echo(heard: str, recent_bot_speech: list[str]) -> bool:
    """True if a transcript is mostly words the bot itself just said."""
    words = [w.lower() for w in _WORD.findall(heard)]
    if len(words) < 3:
        return False
    for said in recent_bot_speech:
        bot_words = {w.lower() for w in _WORD.findall(said)}
        if bot_words and sum(w in bot_words for w in words) / len(words) >= 0.7:
            return True
    return False
