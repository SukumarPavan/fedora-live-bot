import asyncio
import json

import numpy as np
import pytest

from deskbot.config import Settings
from deskbot.engine_common import EngineContext
from deskbot.engine_universal import UniversalEngine
from fake_api import FakeAPI


class Sig:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def emit(self, *a):
        self.log.append((self.name, a))


class FakeBridge:
    def __init__(self):
        self.log = []
        for n in ("state", "emotion", "user_text", "bot_text", "notice", "eyes_failed", "status"):
            setattr(self, n, Sig(self.log, n))


class FakePlayer:
    def __init__(self):
        self.played = []
        self.active = False
        self.cleared = 0
        self.out_rms = 0.0
        self.last_audio_t = 0.0

    def play(self, pcm):
        self.played.append(pcm)
        self.active = True
        asyncio.get_event_loop().call_later(0.2, self._done)

    def _done(self):
        self.active = False

    def clear(self):
        self.cleared += 1
        self.active = False


class Slot:
    def __init__(self, jpeg=None):
        self.jpeg = jpeg

    def get(self, max_age=30):
        return self.jpeg


class Eyes:
    def __init__(self, jpeg=None):
        self.slot = Slot(jpeg)


def make(tmp_path, api, screen_jpeg=None):
    cfg = Settings(tmp_path / "s.json")
    cfg.set("custom_base_url", api.url, save=False)
    cfg.set("keys.custom", "test-key", save=False)
    for k in ("brain.provider", "ears.provider"):
        cfg.set(k, "custom", save=False)
    cfg.set("brain.model", "fake-model", save=False)
    bridge, player = FakeBridge(), FakePlayer()
    ctx = EngineContext(cfg, bridge, player, Eyes(screen_jpeg), Eyes(None),
                        lambda: screen_jpeg is not None, lambda: False)
    eng = UniversalEngine(ctx)

    async def fake_synth(text):
        return np.full(2400, 0.1, dtype=np.float32)
    eng.tts.synth = fake_synth
    return eng, bridge, player


@pytest.mark.asyncio
async def test_full_turn(tmp_path):
    api = FakeAPI()
    eng, bridge, player = make(tmp_path, api, screen_jpeg=b"\xff\xd8fakejpeg\xff\xd9")
    await eng._handle_utterance(np.zeros(16000, dtype=np.int16))
    names = [n for n, _ in bridge.log]
    assert ("user_text", ("hey robot how are you",)) in bridge.log
    assert ("emotion", ("happy", 12.0)) in bridge.log
    final = [a for n, a in bridge.log if n == "bot_text" and a[1]]
    assert final and final[-1][0].startswith("Hello there, friend!") and "[happy]" not in final[-1][0]
    assert len(player.played) >= 2            # spoken sentence by sentence
    assert names.index("state") < names.index("user_text")
    assert ("state", ("speaking",)) in bridge.log and bridge.log[-1] == ("state", ("idle",))
    # the screen image was sent as an OpenAI image_url part
    chat = [json.loads(b) for p, b in api.requests if p.endswith("/chat/completions")][0]
    parts = chat["messages"][-1]["content"]
    assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert eng.history[-1]["role"] == "assistant"
    api.close()


@pytest.mark.asyncio
async def test_hallucination_ignored(tmp_path):
    api = FakeAPI(transcript="Thank you.")
    eng, bridge, player = make(tmp_path, api)
    await eng._handle_utterance(np.zeros(16000, dtype=np.int16))
    assert not player.played and ("state", ("idle",)) in bridge.log
    api.close()


@pytest.mark.asyncio
async def test_barge_in_cancels_reply(tmp_path):
    api = FakeAPI(reply="[neutral] " + "This is a long answer that keeps going. " * 10)
    eng, bridge, player = make(tmp_path, api)

    async def slow_synth(text):
        await asyncio.sleep(0.05)
        return np.full(2400, 0.1, dtype=np.float32)
    eng.tts.synth = slow_synth
    eng._spawn(eng._respond_new("tell me a story"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if player.played:
            break
    assert player.played
    gen = eng.gen
    eng.interrupt()                       # what a detected barge-in does
    await asyncio.sleep(0.3)
    assert eng.gen == gen + 1 and player.cleared >= 1
    n = len(player.played)
    await asyncio.sleep(0.3)
    assert len(player.played) == n        # nothing more is spoken after the interruption
    api.close()


@pytest.mark.asyncio
async def test_proactive_skip_is_silent(tmp_path):
    api = FakeAPI(reply="[skip]")
    eng, bridge, player = make(tmp_path, api, screen_jpeg=b"\xff\xd8x\xff\xd9")
    await eng._respond("check", eng.gen, proactive=True)
    assert not player.played and not eng.history
    api.close()


@pytest.mark.asyncio
async def test_pause_mid_sentence_keeps_first_half(tmp_path):
    api = FakeAPI(transcript="what is the weather")
    eng, bridge, player = make(tmp_path, api)

    async def slow_chat_then_interrupt():
        await eng._handle_utterance(np.zeros(8000, dtype=np.int16))
    eng.busy = True
    t = asyncio.ensure_future(slow_chat_then_interrupt())
    for _ in range(100):                   # wait until the transcript is known, before speech
        await asyncio.sleep(0.01)
        if eng.cur_text:
            break
    eng.interrupt()                        # user resumed talking
    t.cancel()
    assert eng.carry_text == "what is the weather"
    api.transcript = "in Hyderabad today"
    await eng._handle_utterance(np.zeros(8000, dtype=np.int16))
    assert ("user_text", ("what is the weather in Hyderabad today",)) in bridge.log
    api.close()


@pytest.mark.asyncio
async def test_busy_or_hung_model_falls_back_to_backup(tmp_path, monkeypatch):
    from deskbot import llm
    monkeypatch.setattr(llm, "FIRST_TOKEN_TIMEOUT", 1.0)
    llm.ChatOptions.cooldown.clear()
    api = FakeAPI()
    api.fail_models = {"busy-model"}
    api.hang_models = {"hung-model"}
    eng, bridge, player = make(tmp_path, api)
    eng.cfg.set("brain.model", "busy-model", save=False)
    eng.cfg.set("brain.fallback_models", "hung-model, fake-model", save=False)
    await eng._respond("hi", eng.gen)
    final = [a for n, a in bridge.log if n == "bot_text" and a[1]]
    assert final and final[-1][0].startswith("Hello there")
    assert not [a for n, a in bridge.log if n == "notice"]
    # the failed models are tried last next time
    assert llm.model_chain(eng.cfg)[0] == "fake-model"
    api.close()


def test_v1_settings_migrate_to_working_defaults(tmp_path):
    import json as _json
    p = tmp_path / "s.json"
    p.write_text(_json.dumps({"brain": {"provider": "gemini", "model": "gemini-flash-latest",
                                        "reasoning_effort": "none", "max_tokens": 350},
                              "keys": {"gemini": "k"}}))
    cfg = Settings(p).load()
    assert cfg.get("brain.model") == "gemini-flash-lite-latest"
    assert cfg.get("brain.reasoning_effort") == "default"
    assert cfg.get("keys.gemini") == "k" and cfg.get("config_version") == 2
