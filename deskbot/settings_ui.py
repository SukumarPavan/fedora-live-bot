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
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton,
                               QScrollArea, QSlider, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

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


THEME = """
QDialog#settings { background:#14161b; }
QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget { background:#14161b; }
QLabel { color:#d9dce3; }
QLabel#title { color:#ffffff; font-size:20px; font-weight:700; }
QLabel#subtitle, QLabel#hint { color:#8d93a1; }
QListWidget#nav { background:#0e1014; border:none; border-right:1px solid #232733; color:#c4c8d2;
                  font-size:14px; padding:10px 6px; outline:0; }
QListWidget#nav::item { padding:11px 14px; border-radius:9px; margin:2px 4px; }
QListWidget#nav::item:selected { background:#2b1416; color:#ff5d55; font-weight:600; }
QListWidget#nav::item:hover:!selected { background:#191c23; }
QGroupBox { background:#1b1e26; border:1px solid #2a2f3b; border-radius:12px; margin-top:24px;
            padding:16px 14px 12px 14px; color:#ff6a61; font-weight:600; font-size:13px; }
QGroupBox::title { subcontrol-origin:margin; left:14px; top:2px; padding:0 4px; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox { background:#111318; border:1px solid #343a48;
            border-radius:8px; padding:6px 9px; color:#eef0f5; selection-background-color:#c92f29; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus { border:1px solid #ff4d45; }
QComboBox QAbstractItemView { background:#1b1e26; color:#eef0f5; selection-background-color:#3a1a1c;
            border:1px solid #343a48; }
QPushButton { background:#252a35; border:1px solid #3a4150; border-radius:8px; padding:7px 14px;
            color:#eef0f5; }
QPushButton:hover { border-color:#ff4d45; background:#2b303c; }
QPushButton:pressed { background:#1d2129; }
QPushButton#primary { background:#d3302a; border:none; color:#fff; font-weight:700; padding:10px 18px; }
QPushButton#primary:hover { background:#ec3b34; }
QSlider::groove:horizontal { height:6px; background:#2a2f3b; border-radius:3px; }
QSlider::sub-page:horizontal { background:#d3302a; border-radius:3px; }
QSlider::handle:horizontal { background:#f1f2f5; width:16px; margin:-6px 0; border-radius:8px; }
QProgressBar { background:#111318; border:1px solid #343a48; border-radius:6px; }
QProgressBar::chunk { border-radius:5px; background:qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 #2fbf68, stop:0.7 #e9c341, stop:1 #ff4d45); }
QCheckBox, QRadioButton { color:#d9dce3; spacing:9px; }
QCheckBox::indicator, QRadioButton::indicator { width:18px; height:18px; border:1px solid #4a5162;
            background:#111318; }
QCheckBox::indicator { border-radius:5px; }
QRadioButton::indicator { border-radius:9px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background:#d3302a; border-color:#ff5d55; }
QFrame#card { background:#1b1e26; border:1px solid #2a2f3b; border-radius:12px; }
QFrame#card[selected="true"] { border:2px solid #ff4d45; background:#221a1d; }
QScrollBar:vertical { background:#14161b; width:10px; }
QScrollBar::handle:vertical { background:#2f3542; border-radius:5px; min-height:30px; }
QScrollBar::add-line, QScrollBar::sub-line { height:0; }
QToolTip { background:#1b1e26; color:#eef0f5; border:1px solid #3a4150; padding:4px; }
QMessageBox { background:#1b1e26; }
"""

PAGES = [("🧪  Test", "_page_test"), ("🔑  API Keys", "_page_keys"), ("🎙  Talking", "_page_talk"),
         ("🤖  Bot", "_page_bot"), ("👁  Eyes", "_page_eyes"), ("⚙  Advanced", "_page_advanced")]


class SettingsDialog(QDialog):
    def __init__(self, cfg: Settings, ctl):
        super().__init__(None, Qt.Window)
        self.cfg = cfg
        self.ctl = ctl
        self.setObjectName("settings")
        self.setWindowTitle("DeskBot Settings")
        self.setStyleSheet(THEME)
        self.resize(900, 760)
        self._combos: dict[str, QComboBox] = {}
        self._results: dict[str, QLabel] = {}
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(190)
        self.stack = QStackedWidget()
        for label, builder in PAGES:
            self.nav.addItem(label)
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            page = getattr(self, builder)()
            page.setObjectName("page")
            area.setWidget(page)
            self.stack.addWidget(area)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0 if not self._keys_missing() else 1)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.nav)
        lay.addWidget(self.stack, 1)

    def _keys_missing(self) -> bool:
        return not self.cfg.key("gemini") and not self.cfg.key(self.cfg.get("brain.provider"))

    def show_check_tab(self) -> None:
        self.nav.setCurrentRow(0)

    # ------------------------------------------------------------------ page scaffolding
    def _page(self, title: str, subtitle: str, *groups: QWidget) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(26, 22, 26, 22)
        t = QLabel(title)
        t.setObjectName("title")
        st = QLabel(subtitle)
        st.setObjectName("subtitle")
        st.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(st)
        for g in groups:
            lay.addWidget(g)
        lay.addStretch(1)
        return page

    @staticmethod
    def _hint(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("hint")
        lab.setWordWrap(True)
        lab.setOpenExternalLinks(True)
        return lab

    def _result_label(self, key: str, text: str = "not tested yet") -> QLabel:
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setStyleSheet("color:#8d93a1")
        self._results[key] = lab
        return lab

    # ------------------------------------------------------------------ 1. Test
    def _page_test(self) -> QWidget:
        g0, f0 = self._group("Right now")
        state = QLabel()
        state.setWordWrap(True)
        meter = QProgressBar()
        meter.setRange(0, 100)
        meter.setTextVisible(False)
        meter.setFixedHeight(14)
        mic_lab = self._hint("")

        def tick():
            meter.setValue(min(100, int(self.ctl.mic_level * 600)))
            lvl, txt = self.ctl.parts.get("mic", ("", "starting…"))
            mic_lab.setText(txt + ("" if self.cfg.get("listen.mic_on", True) else "   (mic is switched OFF on the bot)"))
            mode = "Gemini Live" if self.cfg.get("engine.mode") == "live" else "Universal"
            elvl, etxt = self.ctl.parts.get("engine", ("ok", "Ready"))
            color = {"ok": "#3ecf6e", "busy": "#5aa6ff", "warn": "#f0b43c", "error": "#ff5a52"}.get(elvl, "#d9dce3")
            state.setText(f"Voice engine: <b>{mode}</b> — <span style='color:{color}'>{etxt}</span>")
        self._test_timer = QTimer(self, interval=80)
        self._test_timer.timeout.connect(tick)
        self._test_timer.start()
        f0.addRow(state)
        f0.addRow("Mic level", self._hbox(meter, self._button("Re-check mic", self.ctl.restart_mic)))
        f0.addRow("", mic_lab)
        f0.addRow(self._hint("Talk now: the bar must jump. If it doesn't, choose your mic on the Talking page."))

        g, f = self._group("Tests")

        def row(key: str, title: str, button: str, fn, help_text: str) -> None:
            b = self._button(button, fn)
            b.setFixedWidth(150)
            box = QWidget()
            v = QVBoxLayout(box)
            v.setContentsMargins(0, 0, 0, 0)
            v.addWidget(self._result_label(key))
            f.addRow(f"<b>{title}</b><br><span style='color:#8d93a1;font-weight:400'>{help_text}</span>",
                     self._hbox(box, b))
        row("keys", "API keys", "Test keys", self._check_keys, "Gemini + Groq")
        row("hearing", "Hearing", "Talk 4 s", self._check_hearing, "mic → speech-to-text")
        row("brain", "Brain", "Test", self._check_brain, "thinking (Universal)")
        row("voice", "Voice", "Test", self._check_voice, "bot speaks a line")
        row("live", "Gemini Live", "Test", self._check_live, "real-time voice")
        row("screen", "Screen sight", "Test", self._check_screen, "GNOME may ask")
        row("camera", "Camera", "Test", self._check_camera, "webcam picture")
        run_all = self._button("Test everything (keys, brain, voice, Live)", self._check_all)
        run_all.setObjectName("primary")
        return self._page("Test", "Check every part of the bot in one place. Green = works. Red = what to fix.",
                          g0, g, run_all)

    def _check_all(self) -> None:
        self._check_keys(lambda: self._check_brain(lambda: self._check_voice(lambda: self._check_live())))

    def _check_keys(self, then=None) -> None:
        wanted = []
        for pid in ("gemini", "groq"):
            if self.cfg.key(pid):
                wanted.append(pid)
        if not wanted:
            self._result("keys", False, "No keys yet. Open the API Keys page.")
            return then() if then else None
        self._result("keys", None, "Checking keys…")

        async def go():
            out = []
            for pid in wanted:
                try:
                    models = await llm.list_models(self.cfg, pid)
                    out.append((pid, True, f"{len(models)} models"))
                except Exception as e:
                    out.append((pid, False, str(e)))
            return out

        def done(res, err):
            if err:
                self._result("keys", False, str(err))
            else:
                ok = all(r[1] for r in res)
                self._result("keys", ok, "  •  ".join(
                    f"{PROVIDERS[p]['label']}: {'works' if good else msg}" for p, good, msg in res))
                for p, good, msg in res:
                    if f"key_{p}" in self._results:
                        self._result(f"key_{p}", good, "Key works" if good else msg)
            if then:
                then()
        run_async(go, done)

    # ------------------------------------------------------------------ 2. API keys
    def _key_row(self, f: QFormLayout, pid: str, used_for: str) -> None:
        meta = PROVIDERS[pid]
        field = self._line(f"keys.{pid}", password=True, placeholder="paste your key here")
        get = self._button("Get key", lambda _=False, u=meta["key_url"]: webbrowser.open(u))
        test = self._button("Test", lambda _=False, p=pid: self._test_one_key(p))
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 6)
        v.addWidget(self._hbox(field, get, test))
        v.addWidget(self._hint(used_for))
        v.addWidget(self._result_label(f"key_{pid}", ""))
        f.addRow(f"<b>{meta['label']}</b>", box)

    def _test_one_key(self, pid: str) -> None:
        if not self.cfg.key(pid):
            return self._result(f"key_{pid}", False, "Paste a key first.")
        self._result(f"key_{pid}", None, "Checking…")
        run_async(lambda: llm.list_models(self.cfg, pid),
                  lambda res, err: self._result(f"key_{pid}", err is None,
                                                str(err) if err else f"Key works ({len(res)} models)."))

    def _page_keys(self) -> QWidget:
        g, f = self._group("Needed (both free)")
        self._key_row(f, "gemini", "Used for: thinking + seeing your screen/camera (Brain) AND Gemini Live mode. "
                                   "Get it at aistudio.google.com/apikey → Create API key.")
        self._key_row(f, "groq", "Used for: hearing you (Whisper speech-to-text) in Universal mode. "
                                 "Get it at console.groq.com/keys → Create API key.")
        g2, f2 = self._group("Optional: other AI providers")
        for pid, used in (("openrouter", "Many free models; pick it as the brain on the Advanced page."),
                          ("openai", "Paid. Brain, hearing or voice."),
                          ("mistral", "Brain."), ("deepseek", "Brain (text only).")):
            self._key_row(f2, pid, used)
        f2.addRow("<b>Custom URL</b>", self._line("custom_base_url", placeholder="https://your-server/v1"))
        f2.addRow("<b>Custom key</b>", self._line("keys.custom", password=True))
        return self._page("API Keys", "Keys stay on this computer only (~/.config/deskbot, readable only by you). "
                          "Click Test next to a key to check it.", g, g2)

    # ------------------------------------------------------------------ 3. Talking
    def _engine_cards(self) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        group = QButtonGroup(box)
        cards = {}
        for mode, title, text in (
                ("universal", "Universal", "Most reliable. Groq hears you, Gemini thinks and sees, free Edge "
                                          "voice speaks. Works with any API."),
                ("live", "Gemini Live", "Real-time natural voice and video, fastest replies. Needs a working "
                                        "Gemini key. Free sessions are short (auto-reconnects).")):
            card = QFrame()
            card.setObjectName("card")
            v = QVBoxLayout(card)
            rb = QRadioButton(title)
            rb.setStyleSheet("font-weight:700; font-size:14px")
            rb.setChecked(self.cfg.get("engine.mode") == mode)
            v.addWidget(rb)
            v.addWidget(self._hint(text))
            group.addButton(rb)
            cards[mode] = (card, rb)
            lay.addWidget(card)

        def paint():
            for m, (c, r) in cards.items():
                c.setProperty("selected", r.isChecked())
                c.style().unpolish(c)
                c.style().polish(c)

        def picked(mode):
            if self.cfg.get("engine.mode") != mode:
                self.ctl.set_engine(mode)
            paint()
        for mode, (card, rb) in cards.items():
            rb.toggled.connect(lambda on, m=mode: on and picked(m))
        paint()
        return box

    def _page_talk(self) -> QWidget:
        g0, f0 = self._group("Voice engine")
        f0.addRow(self._engine_cards())
        g, f = self._group("Microphone and speaker")
        if self.ctl.use_pulse:
            from . import pulse_mic
            default = pulse_mic.default_source()
            mics = [(f"System mic (recommended) — {pulse_mic.describe(default) if default else 'GNOME default'}", "")]
            mics += [(s.description, s.name) for s in pulse_mic.list_sources()
                     if not s.monitor and not s.name.startswith("deskbot_")]
        else:
            mics = [("System mic (recommended)", "")] + [(d, d) for d in list_devices("input")]
        f.addRow("Microphone", self._combo("listen.input_device", mics, on_change=lambda _v: self.ctl.restart_mic()))
        meter = QProgressBar()
        meter.setRange(0, 100)
        meter.setTextVisible(False)
        meter.setFixedHeight(12)
        self._talk_timer = QTimer(self, interval=80)
        self._talk_timer.timeout.connect(lambda: meter.setValue(min(100, int(self.ctl.mic_level * 600))))
        self._talk_timer.start()
        f.addRow("Mic level", meter)
        f.addRow("Speaker", self._combo("listen.output_device",
                                        [("System speaker (recommended)", "")] + [(d, d) for d in list_devices("output")],
                                        on_change=lambda _v: self.ctl.restart_audio()))
        f.addRow("Listening", self._combo("listen.mode", [
            ("Always listening (while the mic button is on)", "always"),
            ("Only when I say its name", "name"),
            ("Click the bot, then talk", "push")]))

        def interrupt_mode(v):
            self.cfg.set("listen.half_duplex", v == "finish")
            self.cfg.set("listen.barge_in", v != "finish")
            self.ctl.engine_settings_changed()
        f.addRow("When I talk while it speaks", self._combo("listen.interrupt_mode", [
            ("Stop and listen to me (smart: ignores its own echo)", "smart"),
            ("Let it finish (mic pauses while it speaks; click the bot to stop it)", "finish")],
            on_change=interrupt_mode))
        g2, f2 = self._group("Its voice")
        f2.addRow("Voice type", self._combo("voice.engine", [
            ("Edge neural voices (free, no key, natural)", "edge"),
            ("OpenAI-compatible speech API", "openai"),
            ("eSpeak (offline, robotic)", "espeak")]))
        self._combo("voice.edge_voice", [(v, v) for v in ("en-US-ChristopherNeural", "en-US-GuyNeural",
                                                          "en-IN-PrabhatNeural", "en-GB-RyanNeural")], editable=True)
        self._combo("voice.edge_voice_alt", [(v, v) for v in ("te-IN-MohanNeural", "te-IN-ShrutiNeural",
                                                              "hi-IN-MadhurNeural", "")], editable=True)
        f2.addRow("English voice", self._hbox(self._combos["voice.edge_voice"],
                                             self._button("All voices", self._load_voices)))
        f2.addRow("Telugu/Hindi voice", self._combos["voice.edge_voice_alt"])
        f2.addRow("Speed", self._slider("voice.rate", -50, 50, 1, "{:+.0f}%"))
        f2.addRow("Pitch", self._slider("voice.pitch", -40, 40, 1, "{:+.0f} Hz"))
        f2.addRow("Volume", self._slider("voice.volume", 0, 200, 5, "{:.0f}%", lambda v: self.ctl.set_volume(v)))
        f2.addRow("Robot effect", self._slider("voice.robot_effect", 0.0, 1.0, 0.05, "{:.2f}"))
        f2.addRow("Gemini Live voice", self._combo("live.voice", [(v, v) for v in LIVE_VOICES], editable=True,
                                                   on_change=lambda _v: self.ctl.restart_engine(only_if="live")))
        preview = self._button("▶  Preview voice", lambda: self.ctl.say(
            "Hello! I am online and ready. నమస్కారం, ఎలా ఉన్నారు?"))
        f2.addRow(preview)
        return self._page("Talking", "How the bot hears you and talks back.", g0, g, g2)

    # ------------------------------------------------------------------ 4. Bot
    def _page_bot(self) -> QWidget:
        look = lambda _v=None: self.ctl.apply_look()  # noqa: E731
        g, f = self._group("Who it is")
        f.addRow("Name", self._line("bot.name"))
        f.addRow("Personality", self._text("bot.personality", 110))
        f.addRow("About me", self._text("bot.about_me", 64, "Things it should know about you: name, job, hobbies…"))
        f.addRow("Language", self._combo("bot.language", [
            ("English", "english"), ("English + Telugu mix", "english_telugu"),
            ("English + Hindi mix", "english_hindi"), ("Any language (auto)", "auto")]))
        g2, f2 = self._group("Look")
        f2.addRow("Size", self._slider("look.size", 0.5, 2.0, 0.05, "{:.2f}x", look))
        f2.addRow("Glow", self._slider("look.glow", 0.0, 1.5, 0.05, "{:.2f}", look))
        f2.addRow("Opacity", self._slider("look.opacity", 0.3, 1.0, 0.05, "{:.2f}", look))
        eye = self._color("look.eye_color")
        eye.clicked.connect(look)
        body = self._color("look.body_tint")
        body.clicked.connect(look)
        f2.addRow("Eye color", eye)
        f2.addRow("Body color", body)
        f2.addRow(self._check("bot.subtitles", "Speech bubble with what it says"))
        f2.addRow(self._check("bot.status_pill", "Status under the bot (Listening, Thinking, errors)"))
        f2.addRow("Bubble stays (s)", self._slider("bot.subtitle_seconds", 2, 20))
        g3, f3 = self._group("Movement")
        f3.addRow("Roaming", self._combo("move.mode", [
            ("Wander around", "wander"), ("Patrol the screen edges", "edges"), ("Stay where I put it", "stay")]))
        f3.addRow("Style", self._combo("move.style", [("Mix", "mix"), ("Hop", "hop"), ("Glide", "glide")]))
        f3.addRow("Move every", self._slider("move.roam_every_s", 5, 180, 1, "{:.0f} s"))
        f3.addRow("Speed", self._slider("move.speed", 0.3, 3.0, 0.1, "{:.1f}x"))
        f3.addRow("Hop height", self._slider("move.hop_height", 0.0, 3.0, 0.1, "{:.1f}x"))
        f3.addRow("Floating", self._slider("move.bob", 0.0, 3.0, 0.1, "{:.1f}x"))
        f3.addRow(self._check("move.still_while_talking", "Stay still while talking"))
        f3.addRow(self._button("Bring the bot back to the corner", self.ctl.bring_back))
        g4, f4 = self._group("Startup")
        f4.addRow(self._check("system.autostart", "Start when I log in", self.ctl.set_autostart))
        f4.addRow("Fall asleep after", self._slider("system.sleep_after_min", 1, 60, 1, "{:.0f} min"))
        return self._page("Bot", "Name, personality, looks and how it moves.", g, g2, g3, g4)

    # ------------------------------------------------------------------ 5. Eyes
    def _page_eyes(self) -> QWidget:
        g, f = self._group("Screen sight")
        f.addRow(self._hint("Turn it on with the screen button next to the bot. On GNOME, pick your screen in "
                            "the dialog the first time; it's remembered after that."))
        f.addRow("Frames per second", self._slider("eyes.screen_fps", 0.2, 3.0, 0.1, "{:.1f}"))
        f.addRow("Detail (max width)", self._slider("eyes.screen_max_width", 640, 1920, 64, "{:.0f} px"))
        f.addRow(self._button("Forget the chosen screen", lambda: (
            self.cfg.set("eyes.restore_token", ""),
            self._info("Screen", "Next time, GNOME will ask which screen to share."))))
        g2, f2 = self._group("Camera")
        f2.addRow("Camera number", self._hbox(self._spin("eyes.cam_index", 0, 9),
                                              self._button("Find cameras", self._detect_cams)))
        f2.addRow(self._button("Show what the bot sees", self._show_frames))
        g3, f3 = self._group("Speaking up on its own")

        def preset(level):
            p = PROACTIVE_PRESETS.get(level)
            if p:
                self.cfg.set("proactive.min_interval_s", p[0])
                self.cfg.set("proactive.change_threshold", p[1])
        f3.addRow("When screen/camera is on", self._combo("proactive.level", [
            ("Sometimes comment on clear changes", "sometimes"), ("Only when I talk", "off"),
            ("Chatty", "chatty")], on_change=preset))
        f3.addRow("At most every", self._slider("proactive.min_interval_s", 15, 900, 15, "{:.0f} s"))
        return self._page("Eyes", "What it can see: your screen and your camera. Pictures stay in memory and go "
                          "only to the AI you chose.", g, g2, g3)

    # ------------------------------------------------------------------ 6. Advanced
    def _page_advanced(self) -> QWidget:
        sc = lambda _v=None: self.ctl.engine_settings_changed()  # noqa: E731
        restart_live = lambda _v=None: self.ctl.restart_engine(only_if="live")  # noqa: E731
        gb, fb = self._group("Brain (Universal mode)")
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
        fb.addRow("Provider", self._combo("brain.provider", PROVIDER_ITEMS, on_change=provider_changed))
        fb.addRow("Model", self._models_row("brain.model", "brain.provider", "chat"))
        fb.addRow("Backup models", backups)
        fb.addRow("Creativity", self._slider("brain.temperature", 0.0, 1.5, 0.05, "{:.2f}"))
        fb.addRow("Max reply length", self._slider("brain.max_tokens", 50, 2000, 25, "{:.0f} tokens"))
        fb.addRow("Memory", self._slider("brain.history_turns", 0, 40, 1, "{:.0f} turns"))
        fb.addRow("Thinking effort", self._combo("brain.reasoning_effort", [
            ("Provider default (recommended)", "default"), ("Off (some models reject this)", "none"),
            ("Low", "low"), ("Medium", "medium"), ("High", "high")]))
        gh, fh = self._group("Hearing (Universal mode)")
        fh.addRow("Speech-to-text provider", self._combo("ears.provider", PROVIDER_ITEMS))
        self._combo("ears.model", self._provider_models(self.cfg.get("ears.provider"), "stt"), editable=True)
        fh.addRow("Model", self._models_row("ears.model", "ears.provider", "stt"))
        fh.addRow("Language hint", self._combo("ears.language", [
            ("Auto detect", ""), ("English", "en"), ("Telugu", "te"), ("Hindi", "hi"), ("Tamil", "ta"),
            ("Kannada", "kn"), ("Malayalam", "ml")], editable=True))
        fh.addRow("Speech detection sensitivity", self._slider("listen.vad_threshold", 0.2, 0.9, 0.05, "{:.2f}", sc))
        fh.addRow("Pause that ends my turn", self._slider("listen.silence_ms", 300, 2000, 50, "{:.0f} ms", sc))
        fh.addRow("Ignore sounds shorter than", self._slider("listen.min_speech_ms", 100, 1000, 50, "{:.0f} ms", sc))
        fh.addRow("Interrupt: voice confidence", self._slider("listen.barge_threshold", 0.5, 0.99, 0.01, "{:.2f}", sc))
        fh.addRow("Interrupt: for at least", self._slider("listen.barge_ms", 100, 1000, 20, "{:.0f} ms", sc))
        fh.addRow("Interrupt: louder than its echo by", self._slider("listen.echo_margin", 1.2, 6.0, 0.1,
                                                                     "{:.1f}x", sc))
        gl, fl = self._group("Gemini Live")
        self._combo("live.model", [("Auto (best Live model for your key)", "auto")], editable=True,
                    on_change=restart_live)
        fl.addRow("Model", self._hbox(self._combos["live.model"], self._button("Fetch models", self._fetch_live_models)))
        fl.addRow("Language", self._combo("live.language_code", [
            ("Automatic", ""), ("English (India)", "en-IN"), ("English (US)", "en-US"),
            ("Telugu", "te-IN"), ("Hindi", "hi-IN")], editable=True, on_change=restart_live))
        fl.addRow("Video frames per second", self._slider("live.video_fps", 0.2, 2.0, 0.1, "{:.1f}"))
        fl.addRow("Start-of-speech sensitivity", self._combo("live.start_sensitivity", [
            ("Low (fewer false starts)", "low"), ("High", "high")], on_change=restart_live))
        fl.addRow("End-of-speech sensitivity", self._combo("live.end_sensitivity", [
            ("Low (waits longer)", "low"), ("High", "high")], on_change=restart_live))
        fl.addRow("Silence before replying", self._slider("live.silence_ms", 200, 2000, 50, "{:.0f} ms", restart_live))
        fl.addRow(self._check("live.robot_effect", "Robot effect on the Live voice"))
        fl.addRow(self._check("live.affective", "Emotion-aware dialog (preview models only)", restart_live))
        gv, fv = self._group("OpenAI-compatible voice (if chosen on Talking)")
        fv.addRow("Provider", self._combo("voice.openai_provider", PROVIDER_ITEMS))
        fv.addRow("Model", self._line("voice.openai_model"))
        fv.addRow("Voice name", self._line("voice.openai_voice"))
        ge, fe = self._group("Screen capture (expert)")
        fe.addRow("Capture method", self._combo("eyes.screen_backend", [
            ("Automatic", "auto"), ("Portal + PipeWire (GNOME/KDE Wayland)", "portal"),
            ("Direct grab (X11 / Windows)", "mss"), ("Custom command", "command")],
            on_change=lambda _v: self.ctl.restart_screen()))
        fe.addRow("Custom command", self._line("eyes.screen_command", placeholder="tool that saves {out}"))
        fe.addRow("Pipeline", self._text("eyes.gst_template", 70))
        gw, fw = self._group("Window (restart the bot after changing)")
        fw.addRow("Window mode", self._combo("window.overlay_mode", [
            ("Overlay: always on top (recommended)", "overlay"), ("Normal window", "normal")]))
        fw.addRow(self._check("window.force_xwayland", "Use XWayland on Wayland (needed to float freely on GNOME)"))
        fw.addRow("Animation FPS", self._slider("look.fps", 15, 60, 5, "{:.0f}", lambda _v: self.ctl.apply_look()))
        gt, ft = self._group("Tools")
        ft.addRow(self._hbox(self._button("Run full self-test", self._selftest),
                             self._button("Open log", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_FILE)))),
                             self._button("Open settings folder",
                                          lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(CONFIG_DIR))))))
        ft.addRow("Log detail", self._combo("system.log_level", [("Normal", "INFO"), ("Debug", "DEBUG")]))

        def reset():
            if QMessageBox.question(self, "Reset", "Reset all settings? Your API keys are kept.") == QMessageBox.Yes:
                self.cfg.reset()
                self._info("Reset", "Done. Reopen Settings to see the defaults.")
                self.close()
        ft.addRow(self._button("Reset everything to defaults", reset))
        return self._page("Advanced", "Fine-tuning. The defaults are good; change only if something misbehaves.",
                          gb, gh, gl, gv, ge, gw, gt)


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
    def _group(title: str) -> tuple[QGroupBox, QFormLayout]:
        g = QGroupBox(title)
        f = QFormLayout(g)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        f.setHorizontalSpacing(16)
        f.setVerticalSpacing(10)
        f.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return g, f

    def _info(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _error(self, title: str, err: Exception) -> None:
        QMessageBox.warning(self, title, str(err))


    # ------------------------------------------------------------------ Check tab
    def _result(self, key: str, ok: bool | None, text: str) -> None:
        lab = self._results[key]
        mark, color = {True: ("✔", "#3ecf6e"), False: ("✘", "#ff5a52"), None: ("⏳", "#9aa0ad")}[ok]
        lab.setText(f"{mark} {text}")
        lab.setStyleSheet(f"color:{color}")

    def _check_hearing(self) -> None:
        self._result("hearing", None, "Recording… talk now for 4 seconds")

        def got(pcm):
            peak = float(abs(pcm.astype("int32")).max()) / 32768 if len(pcm) else 0.0
            if len(pcm) == 0:
                return self._result("hearing", False, "No audio arrived from the mic. Pick another one (Talking page).")
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
                             if ok else "Speech was made but no speaker is open (Talking page → Speaker).")
            if then:
                then()
        run_async(go, done)

    def _check_live(self, then=None) -> None:
        if not self.cfg.key("gemini"):
            self._result("live", False, "No Gemini key. Add it on the API Keys page.")
            return then() if then else None
        self._result("live", None, "Connecting to Gemini Live…")
        from .engine_live import live_selftest

        def done(res, err):
            if err:
                from .engine_live import explain_live_error
                self._result("live", False, explain_live_error(err))
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
                self._result("camera", False, "No picture. Try another camera number (Eyes page).")
                if self.ctl.cam_on and not was_on:
                    self.ctl.set_cam(False)
            else:
                QTimer.singleShot(300, poll)
        QTimer.singleShot(300, poll)


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
            return self._info("Live models", "Paste your Gemini key on the API Keys page first.")
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
