import asyncio

import numpy as np
import pytest
from google.genai import types

from deskbot.config import Settings
from deskbot.engine_common import EngineContext
from deskbot.engine_live import LiveEngine
from test_engine_universal import Eyes, FakeBridge, FakePlayer


class FakeSession:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    async def receive(self):
        while self.messages:
            m = self.messages.pop(0)
            yield m
            if m.server_content and m.server_content.turn_complete:
                return
        await asyncio.sleep(3600)

    async def send_realtime_input(self, **kw):
        self.sent.append(kw)


def make(tmp_path):
    cfg = Settings(tmp_path / "s.json")
    cfg.set("keys.gemini", "k", save=False)
    bridge, player = FakeBridge(), FakePlayer()
    ctx = EngineContext(cfg, bridge, player, Eyes(None), Eyes(None), lambda: False, lambda: False)
    return LiveEngine(ctx), bridge, player, cfg


def test_config_builds(tmp_path):
    eng, *_ = make(tmp_path)
    c = eng._config()
    assert c.response_modalities == ["AUDIO"]
    assert c.speech_config.voice_config.prebuilt_voice_config.voice_name == "Charon"
    assert c.context_window_compression.sliding_window is not None


@pytest.mark.asyncio
async def test_receive_audio_transcripts_and_interrupt(tmp_path):
    eng, bridge, player, _ = make(tmp_path)
    pcm = (np.ones(2400, dtype=np.int16) * 1000).tobytes()
    msgs = [
        types.LiveServerMessage(session_resumption_update=types.LiveServerSessionResumptionUpdate(
            new_handle="h1", resumable=True)),
        types.LiveServerMessage(server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="hello bot"))),
        types.LiveServerMessage(server_content=types.LiveServerContent(
            output_transcription=types.Transcription(text="Haha great to see you!"),
            model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=pcm, mime_type="audio/pcm"))]))),
        types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True)),
        types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True)),
    ]
    task = asyncio.ensure_future(eng._receive(FakeSession(msgs)))
    await asyncio.sleep(0.3)
    task.cancel()
    assert eng.handle == "h1"
    assert ("user_text", ("hello bot",)) in bridge.log
    assert player.played and player.cleared >= 1
    finals = [a for n, a in bridge.log if n == "bot_text" and a[1]]
    assert finals and finals[0][0].startswith("Haha great")
    assert ("emotion", ("happy", 8.0)) in bridge.log


def tone(level):
    t = np.arange(512) / 16000
    return (np.sin(2 * np.pi * 220 * t) * level * 32767 * 1.414).astype(np.int16)


@pytest.mark.asyncio
async def test_barge_gate_blocks_echo_but_passes_real_speech(tmp_path):
    eng, bridge, player, cfg = make(tmp_path)
    probs = []
    eng.vad = lambda block: probs.pop(0)
    session = FakeSession([])
    player.active, player.out_rms = True, 0.05          # bot is talking
    task = asyncio.ensure_future(eng._send_audio(session))
    for _ in range(30):                                  # its echo, even when it sounds like speech
        probs.append(0.95)
        eng.on_mic_block(tone(0.03))
    await asyncio.sleep(0.1)
    assert session.sent == [] and player.cleared == 0
    for _ in range(12):                                  # the user really talks over it
        probs.append(0.97)
        eng.on_mic_block(tone(0.3))
    await asyncio.sleep(0.1)
    task.cancel()
    assert player.cleared >= 1 and session.sent
    assert session.sent[0]["audio"].mime_type == "audio/pcm;rate=16000"
