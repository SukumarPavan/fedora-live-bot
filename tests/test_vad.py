import numpy as np

from deskbot.portal import JpegStreamSplitter
from deskbot.vad import Endpointer, SileroVAD, VadParams


class SeqVAD:
    def __init__(self, probs):
        self.probs = list(probs)

    def __call__(self, block):
        return self.probs.pop(0) if self.probs else 0.0


def blocks(n):
    return [np.zeros(512, dtype=np.int16) for _ in range(n)]


def run(ep, n, speaking=False):
    ev = []
    for b in blocks(n):
        ev += ep.feed(b, bot_speaking=speaking)
    return ev


def test_silero_loads_and_silence_is_quiet():
    v = SileroVAD()
    p = [v(np.zeros(512, dtype=np.int16)) for _ in range(5)]
    assert max(p) < 0.3


def test_utterance_detected_after_silence():
    ep = Endpointer(SeqVAD([0.9] * 20 + [0.0] * 40), VadParams(silence_ms=500, min_speech_ms=200))
    ev = run(ep, 60)
    kinds = [k for k, _ in ev]
    assert kinds == ["start", "utterance"]
    audio = ev[1][1]
    assert len(audio) >= 20 * 512


def test_short_blip_is_noise():
    ep = Endpointer(SeqVAD([0.9] * 4 + [0.0] * 40), VadParams(silence_ms=300, min_speech_ms=300))
    assert [k for k, _ in run(ep, 44)] == ["start", "noise"]


def test_barge_in_needs_stronger_longer_voice():
    p = VadParams(barge_threshold=0.85, barge_ms=300)
    ep = Endpointer(SeqVAD([0.7] * 30), p)
    assert run(ep, 30, speaking=True) == []           # echo-level speech ignored
    ep = Endpointer(SeqVAD([0.95] * 12), p)
    assert [k for k, _ in run(ep, 12, speaking=True)] == ["start"]


def test_jpeg_splitter():
    a = b"\xff\xd8AAA\xff\xd9"
    b = b"\xff\xd8BBBB\xff\xd9"
    sp = JpegStreamSplitter()
    assert sp.feed(b"junk" + a + b[:3]) == [a]
    assert sp.feed(b[3:]) == [b]
