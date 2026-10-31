"""Gemini Live engine: real-time native voice with video frames (screen and/or webcam).

The server detects when you talk and interrupts itself. Because speakers leak the bot's
voice into the mic, audio is only forwarded while the bot speaks if the local VAD hears
real speech (the barge gate)."""
from __future__ import annotations

import asyncio
import collections
import logging
import time

import numpy as np

from .echo_guard import EchoGuard
from .engine_common import EngineContext
from .text_utils import guess_emotion
from .audio_io import pcm16_to_float, robotize
from .vad import make_vad
from .vision import change_score, composite, small_gray

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 15.0
NO_ANSWER_S = 12.0  # you spoke, but no reply audio by then: say so instead of staying silent


DEFAULT_LIVE_MODEL = "gemini-2.5-flash-native-audio-latest"  # answered in ~1.3 s in tests (2026-10)


def explain_live_error(e: BaseException) -> str:
    """Plain-language reason for a Gemini Live failure."""
    from .llm import key_problem
    s = str(e) or type(e).__name__
    low = s.lower()
    bad_key = key_problem(s, "gemini")
    if bad_key:
        return bad_key
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "Gemini Live didn't answer in time (busy or blocked network). Try again in a minute."
    if "quota" in low or "429" in s or "resource_exhausted" in low:
        return "Gemini Live free limit reached. Wait a bit, or switch to Universal mode."
    if "not found" in low or "not supported" in low:
        return "That Live model isn't available for your key. Settings → Advanced → Gemini Live → Model: Auto."
    if "1007" in s or "1011" in s or "1008" in s:
        return f"Gemini Live closed the connection ({s[:100]})."
    return f"Gemini Live problem: {s[:140]}"


async def resolve_live_model(cfg, client) -> str:
    """The configured Live model, or for "auto" the best one this key can use."""
    model = (cfg.get("live.model") or "auto").strip()
    if model != "auto":
        return model
    names = []
    try:
        async for m in await client.aio.models.list():
            if "bidiGenerateContent" in (m.supported_actions or []):
                names.append(m.name.removeprefix("models/"))
    except Exception as e:
        from .llm import key_problem
        if key_problem(str(e), "gemini"):
            raise
        log.warning("Couldn't list Live models (%s); using %s", e, DEFAULT_LIVE_MODEL)
        return DEFAULT_LIVE_MODEL
    if not names:
        raise RuntimeError("no Live-capable Gemini model found for this key (not found)")
    # native-audio models answered in ~1.3 s in tests; plain "live" ones cut in too early
    names.sort(key=lambda n: (0 if "native-audio" in n else 1 if "live" in n else 2, "preview" in n, n))
    log.info("Live models available: %s", names)
    return names[0]


async def live_selftest(cfg, prompt: str = "Say exactly: Live mode works.") -> dict:
    """Connects once, asks for a short spoken reply. Used by Settings → Check."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=cfg.key("gemini"))
    t0 = time.monotonic()
    model = await asyncio.wait_for(resolve_live_model(cfg, client), CONNECT_TIMEOUT_S)
    conf = types.LiveConnectConfig(response_modalities=["AUDIO"],
                                   output_audio_transcription=types.AudioTranscriptionConfig(),
                                   speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                                       prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                           voice_name=cfg.get("live.voice") or "Charon"))))
    audio, said, first = bytearray(), "", None
    async with asyncio.timeout(30):
        async with client.aio.live.connect(model=model, config=conf) as session:
            connected = time.monotonic() - t0
            sent = time.monotonic()
            await session.send_realtime_input(text=prompt)
            async for msg in session.receive():
                sc = msg.server_content
                if not sc:
                    continue
                if sc.output_transcription and sc.output_transcription.text:
                    said += sc.output_transcription.text
                for part in (sc.model_turn.parts if sc.model_turn and sc.model_turn.parts else []):
                    if part.inline_data and part.inline_data.data:
                        first = first or time.monotonic() - sent
                        audio.extend(part.inline_data.data)
                if sc.turn_complete:
                    break
    return {"model": model, "connect_s": connected, "first_audio_s": first, "said": said.strip(),
            "pcm": pcm16_to_float(bytes(audio)) if audio else None}


LIVE_RULES = (
    "You are speaking out loud on a live voice call. Keep it short and natural. "
    "You may receive a live video feed: the user's screen (the small robot on it is you), "
    "sometimes with their webcam as a small picture in the bottom-right corner. React to "
    "what they do and to their face like a friend sitting next to them.")


class LiveEngine:
    def __init__(self, ctx: EngineContext):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.bridge = ctx.bridge
        self.player = ctx.player
        self.q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=400)
        self.text_q: asyncio.Queue[str] = asyncio.Queue()
        self.vad = make_vad()
        self.handle: str | None = None
        self.user_buf = ""
        self.bot_buf = ""
        self.turn_done = True
        self.gate_open = False
        self.gate_run = 0
        self.preroll: collections.deque[np.ndarray] = collections.deque(maxlen=10)
        self.last_proactive = time.monotonic()
        self.last_activity = time.monotonic()
        self.model = ""
        self.guard = EchoGuard(self.player, float(self.cfg.get("listen.echo_margin", 2.5)))
        self.user_spoke_t = 0.0     # last time you were heard and no reply has started yet
        self.warned_no_answer = False
        self.connected = False

    # --- thread-safe entry points (scheduled on our loop by the host) -------------
    def on_mic_block(self, block: np.ndarray) -> None:
        try:
            self.q.put_nowait(block)
        except asyncio.QueueFull:
            pass

    def submit_text(self, text: str) -> None:
        self.interrupt()
        self.text_q.put_nowait(text)

    def push_to_talk(self) -> None:
        self.interrupt()
        self.bridge.state.emit("listening")

    def interrupt(self) -> None:
        self.player.clear()

    def settings_changed(self) -> None:
        self.guard.margin = float(self.cfg.get("listen.echo_margin", 2.5))

    def _status(self, level: str, text: str) -> None:
        self.bridge.status.emit("engine", level, text)

    # --- main loop -------------------------------------------------------------------
    async def run(self) -> None:
        backoff = 2.0
        fails = 0
        while True:
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = self._friendly(e)
                log.warning("Live session ended: %r", e)
                self.connected = False
                fails += 1
                if "not found" in str(e).lower() or "not supported" in str(e).lower():
                    self.model = ""  # re-pick the model next time
                key_bad = "key" in msg.lower() and ("rejected" in msg.lower() or "disabled" in msg.lower())
                if key_bad or "free limit" in msg.lower():
                    self._status("error", "Live: Gemini key problem" if key_bad else "Live: free limit reached")
                    self.bridge.notice.emit(msg)
                    self.bridge.emotion.emit("error", 0)
                    if key_bad:
                        return
                    await asyncio.sleep(60)
                    continue
                if fails <= 1 or fails % 5 == 0:  # don't spam the bubble on flaky networks
                    self.bridge.notice.emit(msg)
                self._status("warn", f"Live reconnecting in {int(backoff)} s…")
            if time.monotonic() - started > 60:
                backoff, fails = 2.0, 0
            self.player.clear()
            self.bridge.state.emit("idle")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _friendly(self, e: Exception) -> str:
        msg = explain_live_error(e)
        if msg.startswith("Gemini Live problem") or msg.startswith("Gemini Live closed"):
            return msg + " Reconnecting…"
        return msg

    async def _resolve_model(self, client) -> str:
        return await resolve_live_model(self.cfg, client)

    def _config(self):
        from google.genai import types
        g = self.cfg.get
        sens = {"low": (types.StartSensitivity.START_SENSITIVITY_LOW, types.EndSensitivity.END_SENSITIVITY_LOW),
                "high": (types.StartSensitivity.START_SENSITIVITY_HIGH, types.EndSensitivity.END_SENSITIVITY_HIGH)}
        speech = types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=g("live.voice") or "Charon")))
        if g("live.language_code"):
            speech.language_code = g("live.language_code")
        kwargs = dict(
            response_modalities=["AUDIO"],
            system_instruction=self.cfg.system_prompt(LIVE_RULES),
            speech_config=speech,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()),
            session_resumption=types.SessionResumptionConfig(handle=self.handle),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=sens.get(g("live.start_sensitivity"), sens["low"])[0],
                    end_of_speech_sensitivity=sens.get(g("live.end_sensitivity"), sens["low"])[1],
                    silence_duration_ms=int(g("live.silence_ms", 600)))),
        )
        if g("live.affective"):
            kwargs["enable_affective_dialog"] = True
        return types.LiveConnectConfig(**kwargs)

    async def _session(self) -> None:
        from google import genai
        key = self.cfg.key("gemini")
        if not key:
            self._status("error", "Live mode needs a Gemini key (Settings → Brain)")
            self.bridge.notice.emit("Live mode needs a Gemini API key. Open Settings → Brain.")
            await asyncio.sleep(3600)
            return
        opts = {"api_version": "v1alpha"} if self.cfg.get("live.affective") else None
        client = genai.Client(api_key=key, http_options=opts)
        self._status("busy", "Live connecting…")
        if not self.model:
            self.model = await asyncio.wait_for(self._resolve_model(client), CONNECT_TIMEOUT_S)
        log.info("Connecting Gemini Live (%s)", self.model)
        cm = client.aio.live.connect(model=self.model, config=self._config())
        session = await asyncio.wait_for(cm.__aenter__(), CONNECT_TIMEOUT_S)
        try:
            self.connected = True
            log.info("Gemini Live connected (%s)", self.model)
            self._status("ok", "Live connected")
            if not self.handle:
                self.bridge.notice.emit("Live mode connected. Talk to me!")
            tasks = [asyncio.ensure_future(t) for t in (
                self._send_audio(session), self._send_video(session),
                self._send_text(session), self._receive(session), self._no_answer_watch())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for t in done:
                    if t.exception():
                        raise t.exception()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.connected = False
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    async def _no_answer_watch(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.user_spoke_t and not self.warned_no_answer and \
                    time.monotonic() - self.user_spoke_t > NO_ANSWER_S:
                self.warned_no_answer = True
                log.warning("Live heard you but sent no reply for %.0f s", NO_ANSWER_S)
                self._status("warn", "Live heard you but isn't answering")
                self.bridge.notice.emit("Gemini Live heard you but isn't answering. It may be busy; "
                                        "try again, or switch to Universal mode (Engine button).")

    async def _send_audio(self, session) -> None:
        from google.genai import types
        g = self.cfg.get
        batch: list[np.ndarray] = []
        while True:
            block = await self.q.get()
            prob = self.vad(block)
            if self.guard.bot_audible():
                if g("listen.half_duplex") or not g("listen.barge_in"):
                    continue
                need = max(1, int(int(g("listen.barge_ms", 300)) / 32))
                is_user = self.guard.is_user(block, prob, float(g("listen.barge_threshold", 0.85)))
                self.gate_run = self.gate_run + 1 if is_user else 0
                self.preroll.append(block)
                if self.gate_run < need:
                    continue
                log.info("Live barge-in")
                self.player.clear()  # stop instantly; the server will also interrupt
                self.bridge.state.emit("listening")
                batch = list(self.preroll)
                self.preroll.clear()
                self.gate_run = 0
            elif prob >= float(g("listen.vad_threshold", 0.5)):
                self.last_activity = time.monotonic()
                if self.turn_done:
                    self.bridge.state.emit("listening")
            batch.append(block)
            if len(batch) >= 3:
                data = np.concatenate(batch).astype(np.int16).tobytes()
                batch = []
                await session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000"))

    async def _send_video(self, session) -> None:
        from google.genai import types
        g = self.cfg.get
        last_gray, last_sent = None, 0.0
        while True:
            await asyncio.sleep(1.0 / max(0.2, float(g("live.video_fps", 1.0))))
            screen = self.ctx.screen.slot.get() if self.ctx.screen_on() else None
            cam = self.ctx.camera.slot.get(max_age=5) if self.ctx.cam_on() else None
            if not screen and not cam:
                last_gray = None
                continue
            frame = await asyncio.to_thread(composite, screen, cam, int(g("eyes.screen_max_width", 1280)),
                                            int(g("eyes.jpeg_quality", 70)))
            gray = small_gray(frame)
            now = time.monotonic()
            # skip identical frames to save tokens, but refresh every 5 s
            if last_gray is not None and change_score(gray, last_gray) < 0.004 and now - last_sent < 5:
                continue
            last_gray, last_sent = gray, now
            await session.send_realtime_input(video=types.Blob(data=frame, mime_type="image/jpeg"))
            await self._maybe_proactive(session, gray)

    async def _maybe_proactive(self, session, gray) -> None:
        g = self.cfg.get
        if g("proactive.level", "off") == "off" or self.player.active or not self.turn_done:
            return
        interval = float(g("proactive.min_interval_s", 120))
        now = time.monotonic()
        if now - self.last_activity < interval or now - self.last_proactive < interval:
            return
        self.last_proactive = now
        await session.send_realtime_input(text=(
            "(Automatic check-in, I didn't speak.) If something on the video is worth a short "
            "friendly remark or question, say one short sentence."))

    async def _send_text(self, session) -> None:
        while True:
            text = await self.text_q.get()
            self.last_activity = time.monotonic()
            await session.send_realtime_input(text=text)

    async def _receive(self, session) -> None:
        robot = self.cfg.get("live.robot_effect")
        while True:
            async for msg in session.receive():
                upd = msg.session_resumption_update
                if upd and upd.resumable and upd.new_handle:
                    self.handle = upd.new_handle
                if msg.go_away:
                    log.info("Live server going away in %s; will resume", msg.go_away.time_left)
                sc = msg.server_content
                if not sc:
                    continue
                if sc.interrupted:
                    self.player.clear()
                    self._finish_turn(interrupted=True)
                    self.bridge.state.emit("listening")
                if sc.input_transcription and sc.input_transcription.text:
                    if self.turn_done:
                        self.user_buf = ""
                        self.turn_done = False
                    self.user_buf += sc.input_transcription.text
                    self.last_activity = time.monotonic()
                    if not self.player.active:
                        self.user_spoke_t = time.monotonic()
                    self.bridge.user_text.emit(self.user_buf.strip())
                if sc.output_transcription and sc.output_transcription.text:
                    self.turn_done = False
                    self.bot_buf += sc.output_transcription.text
                    self.bridge.bot_text.emit(self.bot_buf.strip(), False)
                if sc.model_turn and sc.model_turn.parts:
                    for part in sc.model_turn.parts:
                        data = part.inline_data.data if part.inline_data else None
                        if data:
                            pcm = pcm16_to_float(data)
                            if robot:
                                pcm = robotize(pcm, float(self.cfg.get("voice.robot_effect", 0.25)))
                            if not self.player.active:
                                self.bridge.state.emit("speaking")
                            self.player.play(pcm)
                            self.user_spoke_t = 0.0
                            if self.warned_no_answer:
                                self.warned_no_answer = False
                                self._status("ok", "Live connected")
                if sc.turn_complete:
                    self._finish_turn()

    def _finish_turn(self, interrupted: bool = False) -> None:
        text = self.bot_buf.strip()
        if text:
            emo = guess_emotion(text)
            if emo:
                self.bridge.emotion.emit(emo, 8.0)
            self.bridge.bot_text.emit(text + (" …" if interrupted else ""), True)
        self.bot_buf = ""
        self.turn_done = True
        if not interrupted:
            asyncio.ensure_future(self._idle_after_playback())

    async def _idle_after_playback(self) -> None:
        while self.player.active:
            await asyncio.sleep(0.1)
        if self.turn_done:
            self.bridge.state.emit("idle")
