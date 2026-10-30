"""Text-to-speech: free Edge neural voices, any OpenAI-compatible /audio/speech, espeak-ng.

Every engine returns float32 mono PCM at 24 kHz. If the chosen engine fails, the next
one in the chain is tried, so the bot keeps talking."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time

import numpy as np

from .audio_io import OUT_SR, robotize
from .config import Settings
from .llm import ApiError, friendly_error, make_client
from .text_utils import clean_for_tts, pick_voice

log = logging.getLogger(__name__)


def decode_audio(data: bytes) -> np.ndarray:
    import miniaudio
    d = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16,
                         nchannels=1, sample_rate=OUT_SR)
    return np.frombuffer(d.samples, dtype=np.int16).astype(np.float32) / 32768.0


async def edge_voices() -> list[dict]:
    import edge_tts
    return await edge_tts.list_voices()


class TTS:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.last_error = ""
        self.down_until: dict[str, float] = {}

    async def synth(self, text: str) -> np.ndarray:
        text = clean_for_tts(text)
        if not text:
            return np.zeros(0, dtype=np.float32)
        order = {"edge": ["edge", "openai", "espeak"], "openai": ["openai", "edge", "espeak"],
                 "espeak": ["espeak", "edge"]}.get(self.cfg.get("voice.engine"), ["edge", "espeak"])
        errors = []
        now = time.monotonic()
        healthy = [e for e in order if self.down_until.get(e, 0) <= now]
        for engine in healthy + [e for e in order if e not in healthy]:
            try:
                pcm = await getattr(self, f"_{engine}")(text)
                if len(pcm):
                    if engine != order[0]:
                        log.warning("TTS used fallback %s", engine)
                    self.down_until.pop(engine, None)
                    return robotize(pcm, float(self.cfg.get("voice.robot_effect", 0.0)))
            except Exception as e:
                errors.append(f"{engine}: {e}")
                self.down_until[engine] = time.monotonic() + 300
                log.warning("TTS %s failed (skipping it for 5 min): %s", engine, e)
        self.last_error = "; ".join(errors)
        raise ApiError("I lost my voice (all speech engines failed). Check internet or Settings → Voice.")

    async def _edge(self, text: str) -> np.ndarray:
        import edge_tts
        v = self.cfg.get
        voice = pick_voice(text, v("voice.edge_voice"), v("voice.edge_voice_alt"), v("voice.alt_auto"))
        com = edge_tts.Communicate(text, voice, rate=f"{int(v('voice.rate', 0)):+d}%",
                                   pitch=f"{int(v('voice.pitch', 0)):+d}Hz", connect_timeout=5,
                                   receive_timeout=15)
        chunks = []
        async for ch in com.stream():
            if ch["type"] == "audio":
                chunks.append(ch["data"])
        if not chunks:
            raise RuntimeError("no audio returned")
        return await asyncio.to_thread(decode_audio, b"".join(chunks))

    async def _openai(self, text: str) -> np.ndarray:
        provider = self.cfg.get("voice.openai_provider")
        client = make_client(self.cfg, provider)
        try:
            res = await client.audio.speech.create(
                model=self.cfg.get("voice.openai_model"), voice=self.cfg.get("voice.openai_voice"),
                input=text, response_format="wav")
            data = res.content if hasattr(res, "content") else await res.aread()
        except Exception as e:
            raise ApiError(friendly_error(e, provider)) from e
        finally:
            await client.close()
        return await asyncio.to_thread(decode_audio, data)

    async def _espeak(self, text: str) -> np.ndarray:
        exe = shutil.which("espeak-ng") or shutil.which("espeak")
        if not exe:
            raise RuntimeError("espeak-ng not installed")
        rate = 165 + int(self.cfg.get("voice.rate", 0))
        proc = await asyncio.create_subprocess_exec(
            exe, "-s", str(rate), "-p", "35", "--stdout", text,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return await asyncio.to_thread(decode_audio, out)
