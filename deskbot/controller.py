"""Wires the bot window, audio, eyes, engines and settings together (runs on the UI thread)."""
from __future__ import annotations

import logging
import os
import random
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QApplication, QMenu

from .audio_io import AUDIO_ERROR, Mic, Player
from .bot_window import BotWindow, Bubble, ToolBar
from .bridge import Bridge
from .chat_window import ChatWindow
from .config import APP_ID, Settings
from .engine_common import EngineContext, EngineHost
from .settings_ui import SettingsDialog, ensure_async, run_async
from .vision import Camera, Screen

log = logging.getLogger(__name__)


class Controller(QObject):
    _recorded = Signal(object, object)

    def __init__(self, app: QApplication, cfg: Settings):
        super().__init__()
        ensure_async()
        self.app = app
        self.cfg = cfg
        self.bridge = Bridge()
        self.player = Player()
        self.player.gain = float(cfg.get("voice.volume", 100)) / 100
        self.mic = Mic(self._on_mic_block)
        self._taps: list[list] = []
        self.screen = Screen(cfg, lambda m: self.bridge.eyes_failed.emit("screen:" + m))
        self.camera = Camera(lambda m: self.bridge.eyes_failed.emit("cam:" + m))
        self.screen_on = False
        self.cam_on = False
        self.host = EngineHost()
        self.bot = BotWindow(cfg, lambda: self.player.level)
        self.toolbar = ToolBar(cfg)
        self.bubble = Bubble(cfg)
        self.chat: ChatWindow | None = None
        self.settings_dlg: SettingsDialog | None = None
        self.state = "idle"
        self.last_activity = time.monotonic()
        self.sleeping = False
        self._pending_user = ""
        self._hovering = {"bot": False, "bar": False}

        self._hide_bar = QTimer(self, singleShot=True, interval=1300)
        self._hide_bar.timeout.connect(self._maybe_hide_bar)
        self._sleep_timer = QTimer(self, interval=5000)
        self._sleep_timer.timeout.connect(self._check_sleep)
        self._recorded.connect(lambda cb, pcm: cb(pcm))

        b = self.bridge
        b.state.connect(self._on_state)
        b.emotion.connect(self._on_emotion)
        b.user_text.connect(self._on_user_text)
        b.bot_text.connect(self._on_bot_text)
        b.notice.connect(self._on_notice)
        b.eyes_failed.connect(self._on_eyes_failed)

        self.bot.clicked.connect(self._on_click)
        self.bot.double_clicked.connect(self.open_chat)
        self.bot.menu_requested.connect(self._menu)
        self.bot.hover_changed.connect(lambda on: self._hover("bot", on))
        self.bot.moved.connect(self._follow)
        self.toolbar.hover_changed.connect(lambda on: self._hover("bar", on))
        self.toolbar.action.connect(self._toolbar_action)

    # ------------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.bot.start()
        self._sync_indicators()
        self.restart_audio()
        self.restart_engine()
        self._sleep_timer.start()
        QTimer.singleShot(600, self._greet)

    def _greet(self) -> None:
        name = self.cfg.get("bot.name")
        self.bot.wave()
        self.bot.face.set_emotion("happy", 3)
        mode = self.cfg.get("engine.mode")
        missing = (not self.cfg.key("gemini")) if mode == "live" else \
            (not self.cfg.key(self.cfg.get("brain.provider")) or not self.cfg.key(self.cfg.get("ears.provider")))
        if missing:
            self.bubble.show_text(bot=f"Hi! I'm {name}. Right-click me → Settings → Brain (AI) and paste "
                                      "your free API keys so I can think and talk.", user="")
            QTimer.singleShot(1500, self.open_settings)
        else:
            self.bubble.show_text(bot=f"{name} online. Hover me for buttons, right-click for the menu.",
                                  user="")
        self._follow()

    def quit(self) -> None:
        log.info("Quitting")
        self.host.stop()
        self.mic.stop()
        self.player.stop()
        self.screen.stop()
        self.camera.stop()
        self.cfg.save()
        self.app.quit()

    # ----------------------------------------------------------------------------- audio
    def restart_audio(self) -> None:
        if AUDIO_ERROR:
            self._on_notice(AUDIO_ERROR)
            return
        try:
            self.player.start(self.cfg.get("listen.output_device", ""))
        except Exception as e:
            self._on_notice(f"Speaker problem: {e}")
        try:
            self.mic.start(self.cfg.get("listen.input_device", ""))
        except Exception as e:
            self._on_notice(f"Microphone problem: {e}")

    def _on_mic_block(self, block: np.ndarray) -> None:  # audio thread
        for tap in list(self._taps):
            tap.append(block)
        eng = self.host.engine
        if eng is not None and self.cfg.get("listen.mic_on", True):
            self.host.call(eng.on_mic_block, block)

    def record(self, seconds: float, cb) -> None:
        tap: list = []
        self._taps.append(tap)

        def finish():
            self._taps.remove(tap)
            pcm = np.concatenate(tap) if tap else np.zeros(0, dtype=np.int16)
            self._recorded.emit(cb, pcm)
        QTimer.singleShot(int(seconds * 1000), finish)

    def set_volume(self, v: float) -> None:
        self.player.gain = float(v) / 100

    def say(self, text: str) -> None:
        from .tts import TTS
        run_async(lambda: TTS(self.cfg).synth(text),
                  lambda pcm, err: self._on_notice(str(err)) if err else self.player.play(pcm))

    # --------------------------------------------------------------------------- engine
    def restart_engine(self, only_if: str | None = None) -> None:
        mode = self.cfg.get("engine.mode", "universal")
        if only_if and mode != only_if:
            return
        ctx = EngineContext(self.cfg, self.bridge, self.player, self.screen, self.camera,
                            lambda: self.screen_on, lambda: self.cam_on)
        if mode == "live":
            from .engine_live import LiveEngine
            engine = LiveEngine(ctx)
        else:
            from .engine_universal import UniversalEngine
            engine = UniversalEngine(ctx)
        self.player.clear()
        self.host.start(engine)
        self._sync_indicators()
        log.info("Engine started: %s", mode)

    def engine_settings_changed(self) -> None:
        if self.host.engine:
            self.host.call(self.host.engine.settings_changed)

    # ----------------------------------------------------------------------------- eyes
    def set_screen(self, on: bool) -> None:
        if on == self.screen_on:
            return
        self.screen_on = on
        if on:
            self.screen.start()
            self.bubble.show_text(bot="Screen sight on. I can see your screen now.", user="")
        else:
            self.screen.stop()
        self._sync_indicators()

    def set_cam(self, on: bool) -> None:
        if on == self.cam_on:
            return
        self.cam_on = on
        if on:
            try:
                self.camera.start(int(self.cfg.get("eyes.cam_index", 0)), int(self.cfg.get("eyes.cam_max_width")),
                                  int(self.cfg.get("eyes.jpeg_quality")))
                self.bubble.show_text(bot="Camera on. Hi there!", user="")
                self.bot.face.set_emotion("happy", 2)
            except Exception as e:
                self.cam_on = False
                self._on_notice(str(e))
        else:
            self.camera.stop()
        self._sync_indicators()

    def restart_screen(self) -> None:
        if self.screen_on:
            self.screen.stop()
            self.screen.start()

    def current_frames(self) -> list[tuple[str, bytes]]:
        out = []
        if self.screen_on and self.screen.slot.get():
            out.append(("Screen", self.screen.slot.get()))
        if self.cam_on and self.camera.slot.get():
            out.append(("Camera", self.camera.slot.get()))
        return out

    def _on_eyes_failed(self, what: str) -> None:
        kind, _, msg = what.partition(":")
        if kind == "screen":
            self.screen_on = False
            self.screen.stop()
        else:
            self.cam_on = False
            self.camera.stop()
        self._sync_indicators()
        self._on_notice(msg)

    # ------------------------------------------------------------------------ indicators
    def _sync_indicators(self) -> None:
        f = self.bot.face
        f.screen_on = self.screen_on
        f.cam_on = self.cam_on
        f.mic_on = bool(self.cfg.get("listen.mic_on", True))
        self.toolbar.set_states(screen=self.screen_on, cam=self.cam_on, mic=f.mic_on,
                                live=self.cfg.get("engine.mode") == "live")

    def _on_state(self, s: str) -> None:
        self.state = s
        f = self.bot.face
        f.listening = s == "listening"
        f.speaking = s == "speaking"
        self.bot.talking = s != "idle"
        if s != "idle":
            self._wake()
        if s == "thinking":
            f.set_emotion("thinking")
        elif f.base_emotion == "thinking":
            f.set_emotion("neutral")

    def _on_emotion(self, name: str, hold: float) -> None:
        self._wake()
        self.bot.face.set_emotion(name, hold)

    def _wake(self) -> None:
        self.last_activity = time.monotonic()
        if self.sleeping:
            self.sleeping = False
            self.bot.face.set_emotion("neutral")
            self.bot.face.set_emotion("surprised", 1.0)

    def _check_sleep(self) -> None:
        idle_min = (time.monotonic() - self.last_activity) / 60
        if not self.sleeping and self.state == "idle" and idle_min > float(self.cfg.get("system.sleep_after_min", 10)):
            self.sleeping = True
            self.bot.face.set_emotion("sleepy")

    # ----------------------------------------------------------------------------- text
    def _on_user_text(self, text: str) -> None:
        self._wake()
        self._pending_user = text
        self.bubble.show_text(user=text, bot="", final=False)
        self._follow()

    def _flush_user(self) -> None:
        if self._pending_user and self.chat:
            self.chat.add_user(self._pending_user)
        self._pending_user = ""

    def _on_bot_text(self, text: str, final: bool) -> None:
        self._flush_user()
        self.bubble.show_text(bot=text, final=final)
        if self.chat:
            self.chat.add_bot(text, final)
        self._follow()

    def _on_notice(self, msg: str) -> None:
        log.info("Notice: %s", msg)
        self.bubble.show_text(bot=msg, user="", final=True)
        if self.chat:
            self.chat.add_notice(msg)
        self._follow()

    # ------------------------------------------------------------------------------- UI
    def _follow(self) -> None:
        if self.bubble.isVisible():
            self.bubble.follow(self.bot)
        if self.toolbar.isVisible():
            self.toolbar.follow(self.bot)

    def _hover(self, who: str, on: bool) -> None:
        self._hovering[who] = on
        if any(self._hovering.values()):
            self._hide_bar.stop()
            if not self.toolbar.isVisible():
                self.toolbar.follow(self.bot)
                self.toolbar.show()
                self.toolbar.raise_()
        else:
            self._hide_bar.start()

    def _maybe_hide_bar(self) -> None:
        if not any(self._hovering.values()):
            self.toolbar.hide()

    def _toolbar_action(self, kind: str) -> None:
        if kind == "screen":
            self.set_screen(not self.screen_on)
        elif kind == "cam":
            self.set_cam(not self.cam_on)
        elif kind == "mic":
            self.toggle_mic()
        elif kind == "live":
            self.set_engine("universal" if self.cfg.get("engine.mode") == "live" else "live")
        elif kind == "chat":
            self.open_chat()
        elif kind == "settings":
            self.open_settings()

    def toggle_mic(self) -> None:
        on = not self.cfg.get("listen.mic_on", True)
        self.cfg.set("listen.mic_on", on)
        self.bubble.show_text(bot="I'm listening." if on else "Mic off. I won't listen.", user="")
        self._sync_indicators()

    def set_engine(self, mode: str) -> None:
        self.cfg.set("engine.mode", mode)
        self.restart_engine()
        label = "Gemini Live" if mode == "live" else "Universal"
        self.bubble.show_text(bot=f"Switched to {label} mode.", user="")

    def _on_click(self) -> None:
        self._wake()
        if self.cfg.get("listen.mode") == "push" and self.host.engine:
            self.host.call(self.host.engine.push_to_talk)
            self.bubble.show_text(bot="Listening…", user="", final=True)
            return
        if self.player.active and self.host.engine:
            self.host.call(self.host.engine.interrupt)  # click to shush
            return
        self.bot.face.set_emotion(random.choice(["happy", "wink", "surprised", "love"]), 1.6)
        if random.random() < 0.4:
            self.bot.wave()

    def open_chat(self) -> None:
        if self.chat is None:
            self.chat = ChatWindow(self.cfg.get("bot.name"))
            self.chat.send_text.connect(self._send_text)
            self.chat.clear_memory.connect(self._forget)
        self.chat.show()
        self.chat.raise_()
        self.chat.activateWindow()

    def _send_text(self, text: str) -> None:
        if self.chat:
            self.chat.add_user(text)
        self._pending_user = ""
        self.bubble.show_text(user=text, bot="", final=False)
        self._follow()
        if self.host.engine:
            self.host.call(self.host.engine.submit_text, text)

    def _forget(self) -> None:
        eng = self.host.engine
        if eng is not None and hasattr(eng, "history"):
            eng.history.clear()
        if self.cfg.get("engine.mode") == "live":
            self.restart_engine()
        self._on_notice("Okay, I forgot our conversation.")

    def open_settings(self) -> None:
        if self.settings_dlg is None:
            self.settings_dlg = SettingsDialog(self.cfg, self)
        self.settings_dlg.show()
        self.settings_dlg.raise_()
        self.settings_dlg.activateWindow()

    def apply_look(self) -> None:
        self.bot.apply_look()
        self.toolbar.update()
        self.bubble.update()
        self._follow()

    def bring_back(self) -> None:
        self.cfg.set("window.x", None)
        self.cfg.set("window.y", None)
        self.bot.go_home()
        self._follow()

    def hide_for(self, minutes: int) -> None:
        for w in (self.bot, self.toolbar, self.bubble):
            w.hide()
        QTimer.singleShot(minutes * 60 * 1000, self.bot.show)

    def _menu(self, pos) -> None:
        m = QMenu()
        def act(text, fn, checked=None):
            a = QAction(text, m)
            if checked is not None:
                a.setCheckable(True)
                a.setChecked(checked)
            a.triggered.connect(lambda _=False: fn())
            m.addAction(a)
            return a
        act("Screen sight", lambda: self.set_screen(not self.screen_on), self.screen_on)
        act("Camera", lambda: self.set_cam(not self.cam_on), self.cam_on)
        act("Microphone", self.toggle_mic, bool(self.cfg.get("listen.mic_on", True)))
        m.addSeparator()
        eng = m.addMenu("Voice engine")
        grp = QActionGroup(eng)
        for label, mode in (("Universal (any API)", "universal"), ("Gemini Live", "live")):
            a = QAction(label, eng, checkable=True, checked=self.cfg.get("engine.mode") == mode)
            a.triggered.connect(lambda _=False, md=mode: self.set_engine(md))
            grp.addAction(a)
            eng.addAction(a)
        mv = m.addMenu("Movement")
        grp2 = QActionGroup(mv)
        for label, mode in (("Wander around", "wander"), ("Patrol edges", "edges"), ("Stay here", "stay")):
            a = QAction(label, mv, checkable=True, checked=self.cfg.get("move.mode") == mode)
            a.triggered.connect(lambda _=False, md=mode: self.cfg.set("move.mode", md))
            grp2.addAction(a)
            mv.addAction(a)
        act("Stop talking", lambda: self.host.engine and self.host.call(self.host.engine.interrupt))
        m.addSeparator()
        act("Chat window", self.open_chat)
        act("Settings…", self.open_settings)
        act("Hide for 10 minutes", lambda: self.hide_for(10))
        m.addSeparator()
        act("Quit", self.quit)
        m.exec(pos)

    # ------------------------------------------------------------------------ autostart
    def set_autostart(self, on: bool) -> None:
        try:
            if sys.platform == "win32":
                path = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/deskbot.cmd"
                if on:
                    pyw = Path(sys.executable).with_name("pythonw.exe")
                    root = Path(__file__).resolve().parent.parent
                    path.write_text(f'@echo off\ncd /d "{root}"\nstart "" "{pyw}" -m deskbot\n')
                elif path.exists():
                    path.unlink()
                return
            path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart/deskbot.desktop"
            if on:
                exe = shutil.which(APP_ID) or str(Path.home() / ".local/bin/deskbot")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("[Desktop Entry]\nType=Application\nName=DeskBot\n"
                                f"Exec={exe}\nIcon=deskbot\nX-GNOME-Autostart-enabled=true\n"
                                "X-GNOME-Autostart-Delay=5\n")
            elif path.exists():
                path.unlink()
        except Exception as e:
            self._on_notice(f"Couldn't change autostart: {e}")
