"""No-code settings window: every option is a field, slider, switch or dropdown.

Changes apply immediately and are saved automatically."""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import webbrowser
from contextlib import redirect_stdout
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSlider,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from . import llm
from .audio_io import list_devices
from .config import CONFIG_DIR, LIVE_VOICES, LOG_FILE, PROACTIVE_PRESETS, PROVIDERS, Settings

log = logging.getLogger(__name__)


class _Async(QObject):
    done = Signal(object, object, object)  # callback, result, error

    def __init__(self):
        super().__init__()
        self.done.connect(self.deliver)

    @Slot(object, object, object)
    def deliver(self, cb, res, err) -> None:
        cb(res, err)


_async: _Async | None = None


def ensure_async() -> None:
    """Must first be called on the UI thread so results are delivered there."""
    global _async
    if _async is None:
        _async = _Async()


def run_async(fn: Callable[[], Any], on_done: Callable[[Any, Exception | None], None]) -> None:
    """Run fn (sync, or returning a coroutine) in a thread; call on_done(result, error) in the UI."""
    ensure_async()

    def work():
        try:
            res = fn()
            if asyncio.iscoroutine(res):
                res = asyncio.run(res)
            _async.done.emit(on_done, res, None)
        except Exception as e:  # shown to the user
            _async.done.emit(on_done, None, e)
    threading.Thread(target=work, daemon=True).start()


PROVIDER_ITEMS = [(v["label"], k) for k, v in PROVIDERS.items()]


class SettingsDialog(QDialog):
    def __init__(self, cfg: Settings, ctl):
        super().__init__(None, Qt.Window)
        self.cfg = cfg
        self.ctl = ctl
        self.setWindowTitle("DeskBot Settings")
        self.resize(780, 740)
        self._combos: dict[str, QComboBox] = {}
        tabs = QTabWidget()
        self.tabs = tabs
        for title, builder in [("Check", self._tab_check), ("Bot", self._tab_bot), ("Look && Move", self._tab_look),
                               ("Brain (AI)", self._tab_brain), ("Hearing", self._tab_hearing),
                               ("Voice", self._tab_voice), ("Live mode", self._tab_live),
                               ("Eyes", self._tab_eyes), ("Advanced", self._tab_advanced)]:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(builder())
            tabs.addTab(area, title)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        note = QLabel("Changes apply instantly and save automatically.")
        note.setStyleSheet("color:#888")
        bottom = QHBoxLayout()
        bottom.addWidget(note, 1)
        bottom.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs, 1)
        lay.addLayout(bottom)

    # ------------------------------------------------------------------ widget helpers
    def _line(self, key: str, password: bool = False, placeholder: str = "") -> QWidget:
        w = QLineEdit(str(self.cfg.get(key) or ""))
        w.setPlaceholderText(placeholder)
        w.textChanged.connect(lambda t: self.cfg.set(key, t.strip() if password else t))
        if not password:
            return w
        w.setEchoMode(QLineEdit.Password)
        show = QPushButton("Show")
        show.setCheckable(True)
        show.toggled.connect(lambda on: w.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        return self._hbox(w, show)

    def _text(self, key: str, height: int = 90, placeholder: str = "") -> QPlainTextEdit:
        w = QPlainTextEdit(str(self.cfg.get(key) or ""))
        w.setPlaceholderText(placeholder)
        w.setFixedHeight(height)
        w.textChanged.connect(lambda: self.cfg.set(key, w.toPlainText()))
        return w

    def _check(self, key: str, text: str, on_change: Callable[[bool], None] | None = None) -> QCheckBox:
        w = QCheckBox(text)
        w.setChecked(bool(self.cfg.get(key)))

        def changed(on):
            self.cfg.set(key, bool(on))
            if on_change:
                on_change(bool(on))
        w.toggled.connect(changed)
        return w

    def _combo(self, key: str, items: list[tuple[str, Any]], editable: bool = False,
               on_change: Callable[[Any], None] | None = None) -> QComboBox:
        w = QComboBox()
        w.setEditable(editable)
        cur = self.cfg.get(key)
        for label, value in items:
            w.addItem(label, value)
        idx = w.findData(cur)
        if idx >= 0:
            w.setCurrentIndex(idx)
        elif editable:
            w.setEditText(str(cur or ""))

        def picked(_i=None):
            i = w.currentIndex()
            if i >= 0 and w.currentText() == w.itemText(i) and w.itemData(i) is not None:
                val = w.itemData(i)
            else:  # free text typed into an editable box
                val = w.currentText().strip()
            self.cfg.set(key, val)
            if on_change:
                on_change(val)
        w.currentIndexChanged.connect(picked)
        if editable:
            w.lineEdit().editingFinished.connect(picked)
        self._combos[key] = w
        return w

    def _slider(self, key: str, lo: float, hi: float, step: float = 1.0, fmt: str = "{:.0f}",
                on_change: Callable[[float], None] | None = None) -> QWidget:
        n = int(round((hi - lo) / step))
        s = QSlider(Qt.Horizontal)
        s.setRange(0, n)
        cur = float(self.cfg.get(key) if self.cfg.get(key) is not None else lo)
        s.setValue(int(round((cur - lo) / step)))
        lab = QLabel(fmt.format(cur))
        lab.setMinimumWidth(56)

        def changed(v):
            val = lo + v * step
            val = int(round(val)) if float(step).is_integer() and float(lo).is_integer() else round(val, 4)
            lab.setText(fmt.format(val))
            self.cfg.set(key, val)
            if on_change:
                on_change(val)
        s.valueChanged.connect(changed)
        return self._hbox(s, lab)

    def _spin(self, key: str, lo: int, hi: int) -> QSpinBox:
        w = QSpinBox()
        w.setRange(lo, hi)
        w.setValue(int(self.cfg.get(key) or 0))
        w.valueChanged.connect(lambda v: self.cfg.set(key, int(v)))
        return w

    def _color(self, key: str) -> QPushButton:
        b = QPushButton()

        def paint():
            c = self.cfg.get(key)
            b.setText(c)
            b.setStyleSheet(f"background:{c}; color:{'#000' if QColor(c).lightness() > 128 else '#fff'}")

        def pick():
            c = QColorDialog.getColor(QColor(self.cfg.get(key)), self, "Pick a color")
            if c.isValid():
                self.cfg.set(key, c.name())
                paint()
        b.clicked.connect(pick)
        paint()
        return b

    @staticmethod
    def _hbox(*widgets) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        for w in widgets:
            lay.addWidget(w, 1 if isinstance(w, (QSlider, QLineEdit, QComboBox)) else 0)
        return box

    @staticmethod
    def _button(text: str, fn: Callable[[], None], tip: str = "") -> QPushButton:
        b = QPushButton(text)
        b.clicked.connect(fn)
        if tip:
            b.setToolTip(tip)
        return b

    @staticmethod
    def _page(*groups: QWidget) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        for g in groups:
            lay.addWidget(g)
        lay.addStretch(1)
        return page

    @staticmethod
    def _group(title: str) -> tuple[QGroupBox, QFormLayout]:
        g = QGroupBox(title)
        f = QFormLayout(g)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return g, f

    def _info(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _error(self, title: str, err: Exception) -> None:
        QMessageBox.warning(self, title, str(err))

    # -------------------------------------------------------------------------- tabs
    def show_check_tab(self) -> None:
        self.tabs.setCurrentIndex(0)

    # ------------------------------------------------------------------ Check tab
    def _result(self, key: str, ok: bool | None, text: str) -> None:
        lab = self._results[key]
        mark, color = {True: ("✔", "#2e9d4f"), False: ("✘", "#d03b3b"), None: ("…", "#888")}[ok]
        lab.setText(f"{mark} {text}")
        lab.setStyleSheet(f"color:{color}")

    def _tab_check(self) -> QWidget:
        self._results: dict[str, QLabel] = {}
        g, f = self._group("Is everything working? Click a Test button.")

        def row(key: str, title: str, button: str, fn) -> None:
            lab = QLabel("not tested yet")
            lab.setWordWrap(True)
            lab.setStyleSheet("color:#888")
            self._results[key] = lab
            f.addRow(f"<b>{title}</b>", self._hbox(lab, self._button(button, fn)))

        meter = QProgressBar()
        meter.setRange(0, 100)
        meter.setTextVisible(False)
        meter.setFixedHeight(12)
        mic_lab = QLabel()
        mic_lab.setStyleSheet("color:#888")

        def tick():
            meter.setValue(min(100, int(self.ctl.mic_level * 600)))
            lvl, txt = self.ctl.parts.get("mic", ("", "starting…"))
            mic_lab.setText(txt + ("" if self.cfg.get("listen.mic_on", True) else "  (mic is switched OFF)"))
        self._check_timer = QTimer(self, interval=80)
        self._check_timer.timeout.connect(tick)
        self._check_timer.start()
        f.addRow("<b>Microphone</b>", self._hbox(meter, self._button("Re-check mics", self.ctl.restart_mic)))
        f.addRow("", mic_lab)
        tip = QLabel("Talk: the bar must jump. If it doesn't, pick your mic on the Hearing tab.")
        tip.setStyleSheet("color:#888")
        f.addRow("", tip)
        row("hearing", "Hearing", "Test (talk 4 s)", self._check_hearing)
        row("brain", "Brain", "Test", self._check_brain)
        row("voice", "Voice", "Test", self._check_voice)
        row("live", "Gemini Live", "Test", self._check_live)
        row("screen", "Screen sight", "Test", self._check_screen)
        row("camera", "Camera", "Test", self._check_camera)
        f.addRow(self._button("Run brain + voice + Live checks", self._check_all))
        return self._page(g)

    def _check_all(self) -> None:
        self._check_brain(lambda: self._check_voice(lambda: self._check_live()))

    def _check_hearing(self) -> None:
        self._result("hearing", None, "Recording… talk now for 4 seconds")

        def got(pcm):
            peak = float(abs(pcm.astype("int32")).max()) / 32768 if len(pcm) else 0.0
            if len(pcm) == 0:
                return self._result("hearing", False, "No audio arrived from the mic. Pick another one (Hearing tab).")
            if peak < 0.002:
                return self._result("hearing", False, f"Only silence (peak {peak:.4f}). Mic muted or wrong mic.")
            self._result("hearing", None, "Sending to speech-to-text…")
            t0 = time.monotonic()

            def done(res, err):
                if err:
                    return self._result("hearing", False, str(err))
                from .text_utils import is_hallucination
                if is_hallucination(res or ""):
                    return self._result("hearing", False, f"Heard only noise (loudness {int(peak * 100)}%). "
                                        "Speak closer or raise mic volume.")
                self._result("hearing", True, f"Heard in {time.monotonic() - t0:.1f}s: “{res}”")
            run_async(lambda: llm.transcribe(self.cfg, pcm), done)
        self.ctl.record(4.0, got)

    def _check_brain(self, then=None) -> None:
        self._result("brain", None, "Asking the AI…")
        msgs = [{"role": "system", "content": self.cfg.system_prompt()},
                {"role": "user", "content": "Say hi in at most 8 words."}]

        async def go():
            t0 = time.monotonic()
            return await llm.complete(self.cfg, msgs), time.monotonic() - t0

        def done(res, err):
            if err:
                self._result("brain", False, str(err))
            else:
                text, secs = res
                self._result("brain", True, f"{self.cfg.get('brain.model')} answered in {secs:.1f}s: “{text.strip()}”")
            if then:
                then()
        run_async(go, done)

    def _check_voice(self, then=None) -> None:
        from .tts import TTS
        self._result("voice", None, "Making speech…")
        tts = TTS(self.cfg)

        async def go():
            t0 = time.monotonic()
            pcm = await tts.synth("Voice check. Can you hear me?")
            return pcm, time.monotonic() - t0

        def done(res, err):
            if err:
                self._result("voice", False, str(err))
            else:
                pcm, secs = res
                self.ctl.player.play(pcm)
                fallback = "" if tts.used == self.cfg.get("voice.engine") else \
                    f" (main voice failed, used {tts.used})"
                ok = self.ctl.player.stream is not None
                self._result("voice", ok, f"{tts.used} voice ready in {secs:.1f}s{fallback}. You should hear it now."
                             if ok else "Speech was made but no speaker is open (Hearing tab → Speaker).")
            if then:
                then()
        run_async(go, done)

    def _check_live(self, then=None) -> None:
        if not self.cfg.key("gemini"):
            self._result("live", False, "No Gemini key (Brain tab).")
            return then() if then else None
        self._result("live", None, "Connecting to Gemini Live…")
        from .engine_live import live_selftest

        def done(res, err):
            if err:
                msg = str(err) or type(err).__name__
                if isinstance(err, TimeoutError):
                    msg = "Timed out: Gemini Live didn't answer in 30 s (busy or blocked)."
                self._result("live", False, msg[:200])
            elif res["pcm"] is None:
                self._result("live", False, f"Connected to {res['model']} but it sent no audio.")
            else:
                self.ctl.player.play(res["pcm"])
                self._result("live", True, f"{res['model']}: connected in {res['connect_s']:.1f}s, "
                             f"spoke after {res['first_audio_s']:.1f}s: “{res['said']}”")
            if then:
                then()
        run_async(lambda: live_selftest(self.cfg), done)

    def _check_screen(self) -> None:
        was_on = self.ctl.screen_on
        self.ctl.set_screen(True)
        self._result("screen", None, "Starting… if GNOME asks, pick a screen and click Share")
        t0 = time.monotonic()

        def poll():
            jpeg = self.ctl.screen.slot.get()
            if jpeg:
                self._result("screen", True, f"Got a {len(jpeg) // 1024} KB frame in {time.monotonic() - t0:.0f}s."
                             + ("" if was_on else " (Screen sight left ON; turn it off from the bot.)"))
            elif not self.ctl.screen_on:
                lvl, txt = self.ctl.parts.get("screen", ("", ""))
                self._result("screen", False, txt or "Screen sight failed; see the bot's bubble.")
            elif time.monotonic() - t0 > 100:
                self._result("screen", False, "No frame after 100 s.")
            else:
                QTimer.singleShot(500, poll)
        QTimer.singleShot(500, poll)

    def _check_camera(self) -> None:
        was_on = self.ctl.cam_on
        self.ctl.set_cam(True)
        self._result("camera", None, "Opening camera…")
        t0 = time.monotonic()

        def poll():
            jpeg = self.ctl.camera.slot.get(max_age=5)
            if jpeg:
                self._result("camera", True, f"Got a picture in {time.monotonic() - t0:.1f}s.")
                if not was_on:
                    self.ctl.set_cam(False)
            elif not self.ctl.cam_on or time.monotonic() - t0 > 8:
                self._result("camera", False, "No picture. Try another camera number (Eyes tab).")
                if self.ctl.cam_on and not was_on:
                    self.ctl.set_cam(False)
            else:
                QTimer.singleShot(300, poll)
        QTimer.singleShot(300, poll)

    def _tab_bot(self) -> QWidget:
        g, f = self._group("Identity")
        f.addRow("Name", self._line("bot.name"))
        f.addRow("Personality", self._text("bot.personality", 120))
        f.addRow("About me", self._text("bot.about_me", 70,
                                         "Things the bot should know about you: name, job, hobbies…"))
        f.addRow("Language", self._combo("bot.language", [
            ("English", "english"), ("English + Telugu mix", "english_telugu"),
            ("English + Hindi mix", "english_hindi"), ("Any language (auto)", "auto")]))
        g2, f2 = self._group("Subtitles and startup")
        f2.addRow(self._check("bot.subtitles", "Show speech bubble"))
        f2.addRow(self._check("bot.status_pill", "Show status under the bot (Listening, Thinking, errors)"))
        f2.addRow("Bubble stays (s)", self._slider("bot.subtitle_seconds", 2, 20))
        f2.addRow(self._check("system.autostart", "Start when I log in", self.ctl.set_autostart))
        f2.addRow("Fall asleep after (min idle)", self._slider("system.sleep_after_min", 1, 60))
        return self._page(g, g2)

    def _tab_look(self) -> QWidget:
        look = lambda _v=None: self.ctl.apply_look()  # noqa: E731
        g, f = self._group("Look")
        f.addRow("Size", self._slider("look.size", 0.5, 2.0, 0.05, "{:.2f}x", look))
        f.addRow("Opacity", self._slider("look.opacity", 0.3, 1.0, 0.05, "{:.2f}", look))
        f.addRow("Glow", self._slider("look.glow", 0.0, 1.5, 0.05, "{:.2f}", look))
        eye = self._color("look.eye_color")
        eye.clicked.connect(look)
        body = self._color("look.body_tint")
        body.clicked.connect(look)
        f.addRow("Eye color", eye)
        f.addRow("Body color", body)
        f.addRow("Animation FPS", self._slider("look.fps", 15, 60, 5, "{:.0f}", look))
        g2, f2 = self._group("Movement")
        f2.addRow("Roaming", self._combo("move.mode", [
            ("Wander around", "wander"), ("Patrol the screen edges", "edges"), ("Stay where I put it", "stay")]))
        f2.addRow("Style", self._combo("move.style", [("Mix", "mix"), ("Hop", "hop"), ("Glide", "glide")]))
        f2.addRow("Move every (s)", self._slider("move.roam_every_s", 5, 180))
        f2.addRow("Speed", self._slider("move.speed", 0.3, 3.0, 0.1, "{:.1f}x"))
        f2.addRow("Hop height", self._slider("move.hop_height", 0.0, 3.0, 0.1, "{:.1f}x"))
        f2.addRow("Floating bob", self._slider("move.bob", 0.0, 3.0, 0.1, "{:.1f}x"))
        f2.addRow(self._check("move.still_while_talking", "Stay still while talking"))
        f2.addRow("Screens", self._combo("move.screen", [("Primary screen", "primary"),
                                                         ("All screens", "all"), ("Current screen", "current")]))
        f2.addRow(self._button("Bring the bot back here", self.ctl.bring_back,
                               "Moves the bot to the bottom-right corner"))
        g3, f3 = self._group("Window (restart needed)")
        f3.addRow("Window mode", self._combo("window.overlay_mode", [
            ("Overlay: always on top, no taskbar (recommended)", "overlay"),
            ("Normal window (if overlay misbehaves)", "normal")]))
        f3.addRow(self._check("window.force_xwayland",
                              "Use XWayland on Wayland (needed to float freely on GNOME)"))
        return self._page(g, g2, g3)

    def _models_row(self, combo_key: str, provider_key: str, kind: str) -> QWidget:
        combo = self._combos[combo_key]

        def fetch():
            provider = self.cfg.get(provider_key)

            def done(models, err):
                if err:
                    return self._error("Fetch models", err)
                cur = combo.currentText()
                combo.blockSignals(True)
                combo.clear()
                for m in models:
                    if kind == "stt" and "whisper" not in m and "transcribe" not in m:
                        continue
                    combo.addItem(m, m)
                combo.setEditText(cur)
                combo.blockSignals(False)
                self._info("Models", f"Found {combo.count()} models. Pick one from the list.")
            run_async(lambda: llm.list_models(self.cfg, provider), done)
        return self._hbox(combo, self._button("Fetch models", fetch, "Ask the provider which models exist"))

    def _provider_models(self, provider: str, kind: str) -> list[tuple[str, str]]:
        return [(m, m) for m in PROVIDERS.get(provider, {}).get(f"{kind}_models", [])]

    def _tab_brain(self) -> QWidget:
        g, f = self._group("Engine")
        f.addRow("Voice engine", self._combo("engine.mode", [
            ("Universal: any OpenAI-compatible API (Groq, Gemini, OpenRouter…)", "universal"),
            ("Gemini Live: real-time native voice (needs Gemini key)", "live")],
            on_change=lambda _v: self.ctl.restart_engine()))
        gk, fk = self._group("API keys (stored only on this computer)")
        for pid, meta in PROVIDERS.items():
            if pid == "custom":
                continue
            row = self._hbox(self._line(f"keys.{pid}", password=True, placeholder="paste key"),
                             self._button("Get key", lambda _=False, u=meta["key_url"]: webbrowser.open(u)))
            fk.addRow(meta["label"], row)
        fk.addRow("Custom base URL", self._line("custom_base_url", placeholder="https://host/v1"))
        fk.addRow("Custom key", self._line("keys.custom", password=True))
        gb, fb = self._group("Brain (thinking + seeing)")
        model = self._combo("brain.model", self._provider_models(self.cfg.get("brain.provider"), "chat"),
                            editable=True)

        def provider_changed(p):
            model.blockSignals(True)
            model.clear()
            for label, v in self._provider_models(p, "chat"):
                model.addItem(label, v)
            model.blockSignals(False)
            if model.count():
                model.setCurrentIndex(0)
                self.cfg.set("brain.model", model.currentText())
            backups.setText(", ".join(m for _, m in self._provider_models(p, "chat")[1:3]))
        backups = self._line("brain.fallback_models", placeholder="model-a, model-b")
        backups.setToolTip("Used automatically when the main model is busy, slow or gone")
        fb.addRow("Provider", self._combo("brain.provider", PROVIDER_ITEMS, on_change=provider_changed))
        fb.addRow("Model", self._models_row("brain.model", "brain.provider", "chat"))
        fb.addRow("Backup models", backups)
        fb.addRow("Creativity", self._slider("brain.temperature", 0.0, 1.5, 0.05, "{:.2f}"))
        fb.addRow("Max reply length (tokens)", self._slider("brain.max_tokens", 50, 2000, 25))
        fb.addRow("Memory (turns)", self._slider("brain.history_turns", 0, 40))
        fb.addRow("Thinking effort", self._combo("brain.reasoning_effort", [
            ("Provider default (recommended)", "default"), ("Off (some models reject this)", "none"), ("Low", "low"),
            ("Medium", "medium"), ("High", "high")]))
        fb.addRow(self._button("Test brain", self._test_brain, "Send a tiny test message"))
        return self._page(g, gk, gb)

    def _tab_hearing(self) -> QWidget:
        g, f = self._group("Microphone")
        if self.ctl.use_pulse:
            from . import pulse_mic
            mics = [("Auto: pick a mic that actually hears sound", "")] + [
                (s.description, s.name) for s in pulse_mic.list_sources() if not s.monitor]
        else:
            mics = [("System default", "")] + [(d, d) for d in list_devices("input")]
        f.addRow("Microphone", self._combo("listen.input_device", mics,
                                           on_change=lambda _v: self.ctl.restart_audio()))
        meter = QProgressBar()
        meter.setRange(0, 100)
        meter.setTextVisible(False)
        meter.setFixedHeight(12)
        meter.setToolTip("Live mic level: it should jump when you talk")
        using = QLabel()
        using.setStyleSheet("color:#888")

        def tick():
            meter.setValue(min(100, int(self.ctl.mic_level * 600)))
            using.setText(f"Listening on: {self.ctl.mic_name or 'system default'}")
        self._meter_timer = QTimer(self, interval=80)
        self._meter_timer.timeout.connect(tick)
        self._meter_timer.start()
        f.addRow("Mic level (talk!)", meter)
        f.addRow("", using)
        f.addRow("Speaker", self._combo("listen.output_device",
                                        [("System default", "")] + [(d, d) for d in list_devices("output")],
                                        on_change=lambda _v: self.ctl.restart_audio()))
        f.addRow("Listening", self._combo("listen.mode", [
            ("Always listening (while mic is on)", "always"),
            ("Only when I say its name", "name"),
            ("Click the bot, then talk", "push")]))
        sc = lambda _v=None: self.ctl.engine_settings_changed()  # noqa: E731
        f.addRow("Speech detection sensitivity", self._slider("listen.vad_threshold", 0.2, 0.9, 0.05,
                                                              "{:.2f}", sc))
        f.addRow("Pause that ends my turn (ms)", self._slider("listen.silence_ms", 300, 2000, 50, "{:.0f}", sc))
        f.addRow("Ignore sounds shorter than (ms)", self._slider("listen.min_speech_ms", 100, 1000, 50,
                                                                 "{:.0f}", sc))
        g2, f2 = self._group("Interrupting the bot")
        f2.addRow(self._check("listen.barge_in", "Stop talking when I start talking"))
        f2.addRow("Interrupt needs voice confidence", self._slider("listen.barge_threshold", 0.5, 0.99, 0.01,
                                                                   "{:.2f}", sc))
        f2.addRow("…for at least (ms)", self._slider("listen.barge_ms", 100, 1000, 20, "{:.0f}", sc))
        f2.addRow("With speakers: be louder than its echo by", self._slider(
            "listen.echo_margin", 1.2, 6.0, 0.1, "{:.1f}x", sc))
        f2.addRow(self._check("listen.half_duplex",
                              "Mute the mic while the bot talks (use if it keeps interrupting itself)"))
        g3, f3 = self._group("Speech-to-text (Universal mode)")
        f3.addRow("Provider", self._combo("ears.provider", PROVIDER_ITEMS))
        self._combo("ears.model", self._provider_models(self.cfg.get("ears.provider"), "stt"), editable=True)
        f3.addRow("Model", self._models_row("ears.model", "ears.provider", "stt"))
        f3.addRow("Language hint", self._combo("ears.language", [
            ("Auto detect", ""), ("English", "en"), ("Telugu", "te"), ("Hindi", "hi"), ("Tamil", "ta"),
            ("Kannada", "kn"), ("Malayalam", "ml")], editable=True))
        f3.addRow(self._button("Test hearing (speak for 4 s)", self._test_hearing))
        return self._page(g, g2, g3)

    def _tab_voice(self) -> QWidget:
        g, f = self._group("Voice")
        f.addRow("Engine", self._combo("voice.engine", [
            ("Edge neural voices (free, no key)", "edge"),
            ("OpenAI-compatible speech API", "openai"),
            ("eSpeak (offline, robotic)", "espeak")]))
        self._combo("voice.edge_voice", [(v, v) for v in ("en-US-ChristopherNeural", "en-US-GuyNeural",
                                                          "en-IN-PrabhatNeural", "en-GB-RyanNeural")],
                    editable=True)
        self._combo("voice.edge_voice_alt", [(v, v) for v in ("te-IN-MohanNeural", "te-IN-ShrutiNeural",
                                                              "hi-IN-MadhurNeural", "")], editable=True)
        f.addRow("English voice", self._hbox(self._combos["voice.edge_voice"],
                                             self._button("Load all voices", self._load_voices)))
        f.addRow("Second-language voice", self._combos["voice.edge_voice_alt"])
        f.addRow(self._check("voice.alt_auto", "Switch to the second voice for Telugu/Hindi script"))
        f.addRow("Speed (%)", self._slider("voice.rate", -50, 50))
        f.addRow("Pitch (Hz)", self._slider("voice.pitch", -40, 40))
        f.addRow("Volume (%)", self._slider("voice.volume", 0, 200, 5, "{:.0f}",
                                            lambda v: self.ctl.set_volume(v)))
        f.addRow("Robot effect", self._slider("voice.robot_effect", 0.0, 1.0, 0.05, "{:.2f}"))
        f.addRow(self._button("Preview voice", lambda: self.ctl.say("Hello! I am online and ready. "
                                                                    "నమస్కారం, ఎలా ఉన్నారు?")))
        g2, f2 = self._group("OpenAI-compatible speech (if chosen above)")
        f2.addRow("Provider", self._combo("voice.openai_provider", PROVIDER_ITEMS))
        f2.addRow("Model", self._line("voice.openai_model"))
        f2.addRow("Voice name", self._line("voice.openai_voice"))
        return self._page(g, g2)

    def _tab_live(self) -> QWidget:
        restart = lambda _v=None: self.ctl.restart_engine(only_if="live")  # noqa: E731
        g, f = self._group("Gemini Live (uses your Gemini key)")
        self._combo("live.model", [("Auto (newest Live model)", "auto")], editable=True, on_change=restart)
        f.addRow("Model", self._hbox(self._combos["live.model"],
                                     self._button("Fetch models", self._fetch_live_models)))
        f.addRow("Voice", self._combo("live.voice", [(v, v) for v in LIVE_VOICES], editable=True,
                                      on_change=restart))
        f.addRow("Language code", self._combo("live.language_code", [
            ("Automatic", ""), ("English (India)", "en-IN"), ("English (US)", "en-US"),
            ("Telugu", "te-IN"), ("Hindi", "hi-IN")], editable=True, on_change=restart))
        f.addRow("Video frames per second", self._slider("live.video_fps", 0.2, 2.0, 0.1, "{:.1f}"))
        f.addRow("Start-of-speech sensitivity", self._combo("live.start_sensitivity",
                                                           [("Low (fewer false starts)", "low"), ("High", "high")],
                                                           on_change=restart))
        f.addRow("End-of-speech sensitivity", self._combo("live.end_sensitivity",
                                                         [("Low (waits longer)", "low"), ("High", "high")],
                                                         on_change=restart))
        f.addRow("Silence before replying (ms)", self._slider("live.silence_ms", 200, 2000, 50, "{:.0f}", restart))
        f.addRow(self._check("live.affective", "Emotion-aware dialog (preview models only)", restart))
        f.addRow(self._check("live.robot_effect", "Apply robot effect to the Live voice"))
        note = QLabel("Free-tier Live sessions are short; the bot resumes them automatically. "
                      "Interrupt settings live on the Hearing tab.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#888")
        f.addRow(note)
        return self._page(g)

    def _tab_eyes(self) -> QWidget:
        g, f = self._group("Screen sight")
        f.addRow("Capture method", self._combo("eyes.screen_backend", [
            ("Automatic", "auto"), ("Portal + PipeWire (GNOME/KDE Wayland)", "portal"),
            ("Direct grab (X11 / Windows)", "mss"), ("Custom command", "command")],
            on_change=lambda _v: self.ctl.restart_screen()))
        f.addRow("Custom command", self._line("eyes.screen_command", placeholder="tool that saves {out}"))
        f.addRow("Monitor (direct grab)", self._spin("eyes.screen_monitor", 1, 8))
        f.addRow("Max width (px)", self._slider("eyes.screen_max_width", 640, 1920, 64))
        f.addRow("Frames per second", self._slider("eyes.screen_fps", 0.2, 3.0, 0.1, "{:.1f}"))
        f.addRow(self._button("Forget chosen monitor", lambda: (self.cfg.set("eyes.restore_token", ""),
                                                                self._info("Screen", "Next time you turn on "
                                                                           "Screen sight, GNOME will ask "
                                                                           "which monitor to share."))))
        g2, f2 = self._group("Camera")
        f2.addRow("Camera number", self._hbox(self._spin("eyes.cam_index", 0, 9),
                                              self._button("Detect cameras", self._detect_cams)))
        f2.addRow("Max width (px)", self._slider("eyes.cam_max_width", 320, 1280, 32))
        f2.addRow("JPEG quality", self._slider("eyes.jpeg_quality", 30, 95, 5))
        f2.addRow(self._button("Show what the bot sees", self._show_frames))
        g3, f3 = self._group("Speaking up on its own")

        def preset(level):
            p = PROACTIVE_PRESETS.get(level)
            if p:
                self.cfg.set("proactive.min_interval_s", p[0])
                self.cfg.set("proactive.change_threshold", p[1])
        f3.addRow("Proactive comments", self._combo("proactive.level", [
            ("Sometimes (on clear changes)", "sometimes"), ("Only when I talk", "off"),
            ("Chatty", "chatty")], on_change=preset))
        f3.addRow("At most every (s)", self._slider("proactive.min_interval_s", 15, 900, 15))
        f3.addRow("Change needed", self._slider("proactive.change_threshold", 0.02, 0.4, 0.01, "{:.2f}"))
        return self._page(g, g2, g3)

    def _tab_advanced(self) -> QWidget:
        g, f = self._group("Tools")
        f.addRow(self._button("Run self-test", self._selftest, "Checks audio, screen, camera and API keys"))
        f.addRow(self._button("Open settings folder",
                              lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(CONFIG_DIR)))))
        f.addRow(self._button("Open log file",
                              lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_FILE)))))
        f.addRow("Log detail", self._combo("system.log_level", [("Normal", "INFO"), ("Debug", "DEBUG")]))

        def reset():
            if QMessageBox.question(self, "Reset", "Reset all settings (API keys are kept)?") == QMessageBox.Yes:
                self.cfg.reset()
                self._info("Reset", "Done. Reopen Settings to see the defaults.")
                self.close()
        f.addRow(self._button("Reset to defaults", reset))
        g2, f2 = self._group("Expert: screen pipeline")
        f2.addRow(self._text("eyes.gst_template", 80))
        return self._page(g, g2)

    # ------------------------------------------------------------------------- actions
    def _test_brain(self) -> None:
        msgs = [{"role": "system", "content": self.cfg.system_prompt()},
                {"role": "user", "content": "Say hi in at most 8 words."}]
        run_async(lambda: llm.complete(self.cfg, msgs),
                  lambda res, err: self._error("Brain", err) if err else self._info("Brain works", res))

    def _test_hearing(self) -> None:
        self._info("Test hearing", "Click OK, then speak for 4 seconds.")

        def got(pcm):
            peak = float(abs(pcm.astype("int32")).max()) / 32768 if len(pcm) else 0.0
            mic = self.ctl.mic_name or "the system default mic"
            if len(pcm) == 0:
                return self._info("Test hearing", "No audio arrived from the microphone at all. "
                                  "Pick another mic above.")
            if peak < 0.002:
                return self._info("Test hearing", f"I only got silence from {mic} (peak {peak:.4f}).\n"
                                  "Check the mute switch and input volume in GNOME Settings → Sound, "
                                  "or pick another mic above.")

            def done(res, err):
                if err:
                    return self._error("Hearing", err)
                from .text_utils import is_hallucination
                heard = res or ""
                if is_hallucination(heard):
                    heard = f"(only noise, Whisper guessed: {heard!r})"
                self._info("I heard", f"Mic: {mic}\nLoudness: {int(min(1, peak) * 100)}%\n\n{heard}")
            run_async(lambda: llm.transcribe(self.cfg, pcm), done)
        self.ctl.record(4.0, got)

    def _load_voices(self) -> None:
        from .tts import edge_voices

        def done(voices, err):
            if err:
                return self._error("Voices", err)
            for key in ("voice.edge_voice", "voice.edge_voice_alt"):
                combo = self._combos[key]
                cur = self.cfg.get(key)
                combo.blockSignals(True)
                combo.clear()
                for v in sorted(voices, key=lambda v: v["ShortName"]):
                    combo.addItem(f'{v["ShortName"]} ({v.get("Gender", "")})', v["ShortName"])
                i = combo.findData(cur)
                combo.setCurrentIndex(i) if i >= 0 else combo.setEditText(cur)
                combo.blockSignals(False)
            self._info("Voices", f"Loaded {len(voices)} voices.")
        run_async(edge_voices, done)

    def _fetch_live_models(self) -> None:
        async def fetch():
            from google import genai
            client = genai.Client(api_key=self.cfg.key("gemini"))
            names = []
            async for m in await client.aio.models.list():
                if "bidiGenerateContent" in (m.supported_actions or []):
                    names.append(m.name.removeprefix("models/"))
            return names

        def done(names, err):
            if err:
                return self._error("Live models", err)
            combo = self._combos["live.model"]
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Auto (newest Live model)", "auto")
            for n in names:
                combo.addItem(n, n)
            i = combo.findData(self.cfg.get("live.model"))
            combo.setCurrentIndex(max(0, i))
            combo.blockSignals(False)
            self._info("Live models", "\n".join(names) or "No Live models available for this key.")
        if not self.cfg.key("gemini"):
            return self._info("Live models", "Paste your Gemini key on the Brain tab first.")
        run_async(fetch, done)

    def _detect_cams(self) -> None:
        from .vision import detect_cameras
        run_async(detect_cameras, lambda res, err: self._info(
            "Cameras", f"Working camera numbers: {res}" if res else "No camera found."))

    def _show_frames(self) -> None:
        frames = self.ctl.current_frames()
        if not frames:
            return self._info("Eyes", "Turn on Screen sight or Camera from the bot's buttons first.")
        dlg = QDialog(self)
        dlg.setWindowTitle("What the bot sees right now")
        lay = QVBoxLayout(dlg)
        for name, jpeg in frames:
            img = QImage.fromData(jpeg)
            lab = QLabel()
            lab.setPixmap(QPixmap.fromImage(img).scaledToWidth(min(800, img.width()), Qt.SmoothTransformation))
            lay.addWidget(QLabel(name))
            lay.addWidget(lab)
        dlg.exec()

    def _selftest(self) -> None:
        from . import selftest

        def run():
            buf = io.StringIO()
            with redirect_stdout(buf):
                selftest.run(self.cfg, quick=True)
            return buf.getvalue()

        def done(out, err):
            dlg = QDialog(self)
            dlg.setWindowTitle("Self-test")
            dlg.resize(640, 480)
            t = QPlainTextEdit(out or str(err))
            t.setReadOnly(True)
            QVBoxLayout(dlg).addWidget(t)
            dlg.exec()
        run_async(run, done)
