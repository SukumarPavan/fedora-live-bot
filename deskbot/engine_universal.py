"""Universal voice engine: local VAD → any OpenAI-compatible STT → streaming vision LLM →
sentence-by-sentence TTS. Talking over the bot interrupts it instantly (barge-in)."""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from . import llm
from .config import EMOTIONS
from .echo_guard import EchoGuard, sounds_like_echo
from .engine_common import EngineContext
from .text_utils import (is_hallucination, mentions_name, pop_leading_tag, split_sentences,
                         strip_tags, tag_pending)
from .tts import TTS
from .vad import Endpointer, VadParams, make_vad
from .vision import small_gray, change_score

log = logging.getLogger(__name__)

FACE_RULES = (
    "Start EVERY reply with exactly one tag that sets your robot face: "
    + " ".join(f"[{e}]" for e in EMOTIONS if e not in ("sleepy", "error"))
    + ". Images may be attached to the user's message: 'screen' is the user's screen right "
    "now (the small robot on it is you) and 'camera' is the user's webcam. Use them naturally "
    "like a friend sitting next to them: react to their face and what they are doing, "
    "but do not describe the images unless asked.")

PROACTIVE_PROMPT = (
    "(Automatic check-in: the user did not say anything.) Look at the attached image(s). "
    "Only if something is genuinely worth a short friendly remark or helpful question "
    "(an error on screen, something fun, the user looks tired, happy or stuck), say ONE "
    "short sentence. Otherwise reply with exactly [skip].")

FOLLOW_UP_WINDOW_S = 20.0
TURN_DEADLINE_S = 20.0   # from end of your speech to the first word of the reply


class UniversalEngine:
    def __init__(self, ctx: EngineContext):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.bridge = ctx.bridge
        self.player = ctx.player
        self.q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=400)
        self.ep = Endpointer(make_vad(), self._vad_params())
        self.guard = EchoGuard(self.player, float(self.cfg.get("listen.echo_margin", 2.5)))
        self.recent_speech: list[tuple[float, str]] = []   # what the bot said lately (echo check)
        self.turn_t = 0.0
        self.turn_proactive = False
        self.tts = TTS(self.cfg)
        self.history: list[dict] = []
        self.gen = 0
        self.busy = False
        self.ptt_armed = False
        self.task: asyncio.Task | None = None
        self.last_activity = time.monotonic()
        self.last_reply = 0.0
        self.last_proactive = 0.0
        self.seen_gray = None
        # If you pause mid-sentence and continue, the bot is interrupted before it answered:
        # keep what you already said and prepend it to the next utterance.
        self.carry_audio: np.ndarray | None = None
        self.carry_text = ""
        self.cur_audio: np.ndarray | None = None
        self.cur_text = ""
        self.reply_started = False

    def _vad_params(self) -> VadParams:
        g = self.cfg.get
        return VadParams(threshold=float(g("listen.vad_threshold")), silence_ms=int(g("listen.silence_ms")),
                         min_speech_ms=int(g("listen.min_speech_ms")),
                         barge_threshold=float(g("listen.barge_threshold")), barge_ms=int(g("listen.barge_ms")))

    # --- called from other threads (via the host loop) ---------------------------
    def on_mic_block(self, block: np.ndarray) -> None:
        try:
            self.q.put_nowait(block)
        except asyncio.QueueFull:
            pass

    def submit_text(self, text: str) -> None:
        self.interrupt()
        self.carry_audio = None
        self.carry_text = ""
        self._spawn(self._respond_new(text))

    def push_to_talk(self) -> None:
        self.interrupt()
        self.ptt_armed = True
        self.bridge.state.emit("listening")

    def settings_changed(self) -> None:
        self.ep.p = self._vad_params()
        self.guard.margin = float(self.cfg.get("listen.echo_margin", 2.5))

    def interrupt(self) -> None:
        if self.busy and not self.reply_started:
            if self.cur_text:
                self.carry_text = self.cur_text
            elif self.cur_audio is not None:
                self.carry_audio = self.cur_audio
        self.gen += 1
        self.player.clear()
        if self.task and not self.task.done():
            self.task.cancel()
        self.busy = False

    # --- loops ---------------------------------------------------------------------
    async def run(self) -> None:
        missing = [p for p in {self.cfg.get("brain.provider"), self.cfg.get("ears.provider")}
                   if not self.cfg.key(p)]
        if missing:
            self._status("error", f"No API key for {', '.join(missing)} (Settings → Brain)")
        else:
            self._status("ok", "Ready")
        await asyncio.gather(self._mic_loop(), self._proactive_loop(), self._watchdog())

    def _status(self, level: str, text: str) -> None:
        self.bridge.status.emit("engine", level, text)

    async def _watchdog(self) -> None:
        """Never sit 'thinking' forever: give up loudly after TURN_DEADLINE_S."""
        while True:
            await asyncio.sleep(1.0)
            if self.busy and not self.reply_started and self.turn_t and \
                    time.monotonic() - self.turn_t > TURN_DEADLINE_S:
                log.warning("No reply within %.0f s; giving up on this turn", TURN_DEADLINE_S)
                quiet = self.turn_proactive
                self.cur_text, self.cur_audio = "", None
                self.interrupt()
                self.turn_t = 0.0
                if not quiet:
                    self._fail("Sorry, I couldn't get an answer in time. The AI service is slow or busy. "
                               "Try again, or pick another model in Settings → Brain.")

    async def _mic_loop(self) -> None:
        g = self.cfg.get
        while True:
            block = await self.q.get()
            speaking = self.guard.bot_audible()
            if speaking and (g("listen.half_duplex") or not g("listen.barge_in")):
                continue
            if g("listen.mode") == "push" and not self.ptt_armed:
                continue
            for kind, audio in self.ep.feed(block, bot_speaking=speaking, gate=self.guard.is_user):
                if kind == "start":
                    log.info("Speech started (voice probability %.2f)", self.ep.last_prob)
                    self.last_activity = time.monotonic()
                    if speaking or self.busy:
                        log.info("Barge-in: user started talking")
                        self.interrupt()
                    self.bridge.state.emit("listening")
                elif kind == "utterance":
                    log.info("Speech ended: %.1f s of audio, sending to speech-to-text", len(audio) / 16000)
                    self.ptt_armed = False
                    self._spawn(self._handle_utterance(audio))
                elif not self.busy:
                    self.bridge.state.emit("idle")

    def _spawn(self, coro) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = asyncio.ensure_future(coro)

    async def _handle_utterance(self, audio: np.ndarray) -> None:
        if self.carry_audio is not None:
            gap = np.zeros(3200, dtype=np.int16)
            audio = np.concatenate([self.carry_audio, gap, audio])
            self.carry_audio = None
        self.busy = True
        self.reply_started = False
        self.turn_t, self.turn_proactive = time.monotonic(), False
        self.cur_audio, self.cur_text = audio, ""
        gen = self.gen
        self.bridge.state.emit("thinking")
        try:
            text = await llm.transcribe(self.cfg, audio)
        except llm.ApiError as e:
            self._fail(str(e))
            return
        if gen != self.gen:
            return
        log.info("Heard: %r", text)
        if is_hallucination(text):
            log.info("Ignored noise transcript: %r", text)
            text = ""
        if self.carry_text:
            text = f"{self.carry_text} {text}".strip()
            self.carry_text = ""
        if not text:
            self._idle()
            return
        now = time.monotonic()
        self.recent_speech = [(t, x) for t, x in self.recent_speech if now - t < 30]
        if sounds_like_echo(text, [x for _, x in self.recent_speech]):
            log.info("Ignored my own voice coming back through the mic: %r", text)
            self._idle()
            return
        self.cur_text = text
        if self.cfg.get("listen.mode") == "name" and \
                time.monotonic() - self.last_reply > FOLLOW_UP_WINDOW_S and \
                not mentions_name(text, self.cfg.get("bot.name", "")):
            log.info("Not addressed by name: %r", text)
            self._idle()
            return
        self.bridge.user_text.emit(text)
        await self._respond(text, gen)

    async def _respond_new(self, text: str) -> None:
        self.busy = True
        self.reply_started = False
        self.turn_t, self.turn_proactive = time.monotonic(), False
        await self._respond(text, self.gen)

    def _images(self) -> list[tuple[str, bytes]]:
        out = []
        if self.ctx.screen_on():
            jpeg = self.ctx.screen.slot.get()
            if jpeg:
                out.append(("screen", jpeg))
        if self.ctx.cam_on():
            jpeg = self.ctx.camera.slot.get(max_age=5)
            if jpeg:
                out.append(("camera", jpeg))
        return out

    def _messages(self, text: str, images: list[tuple[str, bytes]]) -> list[dict]:
        keep = int(self.cfg.get("brain.history_turns", 12)) * 2
        msgs = [{"role": "system", "content": self.cfg.system_prompt(FACE_RULES)}]
        msgs += self.history[-keep:]
        if images:
            label = " and ".join(name for name, _ in images)
            content = [{"type": "text", "text": f"{text}\n\n[attached: {label}]"}]
            content += [llm.image_part(j) for _, j in images]
            msgs.append({"role": "user", "content": content})
        else:
            msgs.append({"role": "user", "content": text})
        return msgs

    async def _respond(self, text: str, gen: int, proactive: bool = False) -> None:
        self.busy = True
        images = self._images()
        messages = self._messages(text, images)
        say_q: asyncio.Queue[str | None] = asyncio.Queue()
        speaker = asyncio.ensure_future(self._speak(say_q, gen))
        full, buf, tag_done, skipped = "", "", False, False
        try:
            if not proactive:
                self.bridge.state.emit("thinking")
            async for delta in llm.chat_stream(self.cfg, messages):
                if gen != self.gen:
                    break
                full += delta
                if not tag_done:
                    if tag_pending(full):
                        continue
                    tag, rest = pop_leading_tag(full)
                    tag_done = True
                    if tag == "skip":
                        skipped = True
                        break
                    if tag in EMOTIONS:
                        self.bridge.emotion.emit(tag, 12.0)
                    buf = rest
                else:
                    buf += delta
                if "[skip]" in full.lower() and proactive:
                    skipped = True
                    break
                sentences, buf = split_sentences(buf)
                for s in sentences:
                    await say_q.put(s)
                self.bridge.bot_text.emit(strip_tags(full), False)
            if not skipped and buf.strip() and gen == self.gen:
                await say_q.put(buf.strip())
        except llm.ApiError as e:
            if gen == self.gen:
                self._fail(str(e), keep_speaking=True)
        except asyncio.CancelledError:
            speaker.cancel()
            raise
        finally:
            await say_q.put(None)
        try:
            await speaker
        except asyncio.CancelledError:
            pass
        except llm.ApiError as e:
            self._fail(str(e))
            return
        if skipped:
            log.info("Proactive check: nothing to say")
            self._idle()
            return
        reply = strip_tags(full)
        if gen != self.gen and reply:
            reply += " …(interrupted)"
        if reply:
            note = f" [attached: {', '.join(n for n, _ in images)}]" if images else ""
            if not proactive:
                self.history.append({"role": "user", "content": text + note})
            else:
                self.history.append({"role": "user", "content": "(check-in)" + note})
            self.history.append({"role": "assistant", "content": reply})
            self.bridge.bot_text.emit(reply, True)
            self.recent_speech.append((time.monotonic(), reply))
            log.info("Replied: %r", reply[:200])
            self._status("ok", "Ready")
            vision_note = llm.vision_note(self.cfg)
            if vision_note and images:
                self.bridge.notice.emit(vision_note)
        self.last_reply = time.monotonic()
        if gen == self.gen:
            self.cur_audio, self.cur_text = None, ""
            self._idle()

    async def _speak(self, say_q: asyncio.Queue, gen: int) -> None:
        first = True
        while True:
            s = await say_q.get()
            if s is None or gen != self.gen:
                break
            pcm = await self.tts.synth(s)
            if gen != self.gen:
                break
            if first:
                self.bridge.state.emit("speaking")
                self.reply_started = True
                first = False
            self.player.play(pcm)
        while self.player.active and gen == self.gen:
            await asyncio.sleep(0.05)

    def _idle(self) -> None:
        self.busy = False
        self.turn_t = 0.0
        self.bridge.state.emit("idle")

    def _fail(self, msg: str, keep_speaking: bool = False) -> None:
        log.warning("Engine error: %s", msg)
        self.bridge.notice.emit(msg)
        self._status("error", msg)
        self.bridge.emotion.emit("error", 3.0)
        if not keep_speaking:
            self._idle()

    async def _proactive_loop(self) -> None:
        g = self.cfg.get
        while True:
            await asyncio.sleep(4)
            if g("proactive.level", "off") == "off":
                continue
            if not (self.ctx.screen_on() or self.ctx.cam_on()):
                self.seen_gray = None
                continue
            if self.busy or self.player.active or self.ep.in_speech:
                continue
            interval = float(g("proactive.min_interval_s", 120))
            now = time.monotonic()
            if now - self.last_activity < interval or now - self.last_proactive < interval \
                    or now - self.last_reply < interval:
                continue
            frame = self.ctx.screen.slot.get() if self.ctx.screen_on() else None
            frame = frame or self.ctx.camera.slot.get(max_age=5)
            gray = small_gray(frame) if frame else None
            if gray is None:
                continue
            if self.seen_gray is None:
                self.seen_gray = gray
                continue
            score = change_score(gray, self.seen_gray)
            if score < float(g("proactive.change_threshold", 0.12)):
                continue
            log.info("Proactive check (change %.3f)", score)
            self.seen_gray = gray
            self.last_proactive = now
            self.reply_started = False
            self.turn_t, self.turn_proactive = time.monotonic(), True
            self.task = asyncio.ensure_future(self._respond(PROACTIVE_PROMPT, self.gen, proactive=True))
