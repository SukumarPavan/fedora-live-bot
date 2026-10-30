"""The floating bot window, its hover toolbar and the speech bubble."""
from __future__ import annotations

import math
import random
import sys
import time

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import QToolTip, QWidget

from .art import Face, RobotPainter
from .config import Settings

BOT_W, BOT_H = 160, 210     # window size at scale 1.0 (design box 200x250 scaled down)
BOB_MARGIN = 10


def overlay_flags(mode: str) -> Qt.WindowType:
    flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
             | Qt.WindowDoesNotAcceptFocus)
    if mode == "overlay" and sys.platform.startswith("linux"):
        flags |= Qt.X11BypassWindowManagerHint
    return flags


def _setup_overlay(w: QWidget, mode: str) -> None:
    w.setWindowFlags(overlay_flags(mode))
    w.setAttribute(Qt.WA_TranslucentBackground, True)
    w.setAttribute(Qt.WA_NoSystemBackground, True)
    w.setAttribute(Qt.WA_ShowWithoutActivating, True)


class Mover:
    """Roaming: idle bobbing, hops or glides to random spots on the screen."""

    def __init__(self):
        self.pos = QPointF(0, 0)
        self.target: QPointF | None = None
        self.hop_from = QPointF(0, 0)
        self.hop_to = QPointF(0, 0)
        self.hop_t = 1.0
        self.hop_dur = 0.45
        self.hop_h = 30.0
        self.lift = 0.0
        self.squash = 0.0
        self.lean = 0.0
        self.gliding = False
        self.next_roam = random.uniform(5, 12)

    def busy(self) -> bool:
        return self.target is not None


class BotWindow(QWidget):
    clicked = Signal()
    double_clicked = Signal()
    menu_requested = Signal(QPoint)
    hover_changed = Signal(bool)
    moved = Signal()

    def __init__(self, cfg: Settings, level_source, mic_source=lambda: 0.0):
        super().__init__(None)
        self.cfg = cfg
        self.level_source = level_source
        self.mic_source = mic_source
        self.face = Face()
        self.painter = RobotPainter()
        self.mover = Mover()
        self.talking = False
        self._drag_off: QPoint | None = None
        self._press_pos = QPoint()
        self._dragged = False
        self._last_cursor = QPoint()
        self._cursor_seen = 0.0
        self._last = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        _setup_overlay(self, cfg.get("window.overlay_mode", "overlay"))
        self.setMouseTracking(True)
        self.apply_look()
        self._place_initial()

    # --- configuration -----------------------------------------------------------
    def scale(self) -> float:
        return float(self.cfg.get("look.size", 1.0))

    def apply_look(self) -> None:
        g = self.cfg.get
        self.painter.set_colors(g("look.eye_color"), g("look.body_tint"), float(g("look.glow")))
        s = self.scale()
        self.setFixedSize(int(BOT_W * s), int((BOT_H + BOB_MARGIN * 2) * s))
        self.setWindowOpacity(1.0)
        self.timer.setInterval(int(1000 / max(10, int(g("look.fps", 45)))))
        self.update()

    def screen_rect(self) -> QRect:
        screens = QGuiApplication.screens()
        if self.cfg.get("move.screen") == "all" and len(screens) > 1:
            r = QRect()
            for s in screens:
                r = r.united(s.availableGeometry())
            return r
        center = self.geometry().center()
        scr = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        if self.cfg.get("move.screen") == "primary":
            scr = QGuiApplication.primaryScreen()
        return scr.availableGeometry()

    def _place_initial(self) -> None:
        x, y = self.cfg.get("window.x"), self.cfg.get("window.y")
        r = self.screen_rect()
        if x is None or y is None:
            x, y = r.right() - self.width() - 60, r.bottom() - self.height() - 40
        x = min(max(int(x), r.left()), r.right() - self.width())
        y = min(max(int(y), r.top()), r.bottom() - self.height())
        self.mover.pos = QPointF(x, y)
        self.move(int(x), int(y))

    def start(self) -> None:
        self.show()
        self.timer.start()

    # --- animation loop ----------------------------------------------------------------
    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self._last)
        self._last = now
        f = self.face
        lvl = float(self.level_source() or 0.0)
        f.mouth_level += (lvl - f.mouth_level) * 0.5
        mic = float(self.mic_source() or 0.0)
        f.hear_level += (mic - f.hear_level) * (0.5 if mic > f.hear_level else 0.15)
        self._track_cursor(now)
        f.update(dt)
        self._move_tick(dt)
        self.update()

    def _track_cursor(self, now: float) -> None:
        pos = QCursor.pos()
        if pos != self._last_cursor:
            self._last_cursor = pos
            self._cursor_seen = now
        if now - self._cursor_seen < 2.5 and not self.face.speaking:
            c = self.geometry().center()
            dx, dy = pos.x() - c.x(), pos.y() - (c.y() - self.height() * 0.15)
            d = math.hypot(dx, dy)
            if d < 700:
                self.face.forced_look = (max(-1, min(1, dx / 250)), max(-1, min(1, dy / 250)))
                return
        self.face.forced_look = None

    def _move_tick(self, dt: float) -> None:
        m = self.mover
        g = self.cfg.get
        m.squash = max(0.0, m.squash - dt * 5)
        if self._drag_off is not None:
            return
        mode = g("move.mode", "wander")
        if m.target is None:
            m.lean += (0 - m.lean) * min(1, dt * 6)
            if mode == "stay" or (self.talking and g("move.still_while_talking", True)):
                return
            m.next_roam -= dt
            if m.next_roam <= 0:
                every = float(g("move.roam_every_s", 25))
                m.next_roam = every * random.uniform(0.6, 1.4)
                self._pick_target(mode)
            return
        style = g("move.style", "mix")
        speed = float(g("move.speed", 1.0))
        if m.hop_t < 1.0:
            m.hop_t = min(1.0, m.hop_t + dt / m.hop_dur)
            t = m.hop_t
            m.pos = m.hop_from + (m.hop_to - m.hop_from) * t
            m.lift = 4 * m.hop_h * t * (1 - t)
            if m.hop_t >= 1.0:
                m.squash = 1.0
                m.lift = 0
        elif m.gliding:
            d = m.target - m.pos
            dist = math.hypot(d.x(), d.y())
            step = 140 * speed * dt * self.scale()
            if dist <= step:
                m.pos = QPointF(m.target)
                m.target = None
                m.gliding = False
            else:
                m.pos += d * (step / dist)
                m.lean += (max(-12, min(12, d.x() / dist * 12)) - m.lean) * min(1, dt * 4)
        else:
            d = m.target - m.pos
            dist = math.hypot(d.x(), d.y())
            if dist < 2:
                m.target = None
            else:
                use_hop = style == "hop" or (style == "mix" and random.random() < 0.6)
                if not use_hop:
                    m.gliding = True
                else:
                    hop = min(dist, 110 * self.scale() * speed)
                    m.hop_from = QPointF(m.pos)
                    m.hop_to = m.pos + d * (hop / dist)
                    m.hop_t = 0.0
                    m.hop_dur = 0.42 / max(0.3, speed) ** 0.5
                    m.hop_h = 34 * float(g("move.hop_height", 1.0)) * self.scale()
                    m.lean = max(-10, min(10, d.x() / dist * 10))
        self.move(int(m.pos.x()), int(m.pos.y() - m.lift))
        self.moved.emit()

    def _pick_target(self, mode: str) -> None:
        r = self.screen_rect()
        w, h = self.width(), self.height()
        x0, x1 = r.left(), r.right() - w
        y0, y1 = r.top() + 20, r.bottom() - h
        if x1 <= x0 or y1 <= y0:
            return
        cur = self.mover.pos
        if mode == "edges":
            side = random.choice(["bottom", "bottom", "left", "right"])
            if side == "bottom":
                x, y = random.uniform(x0, x1), y1
            else:
                x, y = (x0 if side == "left" else x1), random.uniform(y0 + (y1 - y0) * 0.4, y1)
        else:
            reach_x, reach_y = (x1 - x0) * 0.35, (y1 - y0) * 0.35
            x = min(max(cur.x() + random.uniform(-reach_x, reach_x), x0), x1)
            y = min(max(cur.y() + random.uniform(-reach_y, reach_y), y0), y1)
        self.mover.target = QPointF(x, y)

    def go_home(self) -> None:
        self.mover.target = None
        self._place_initial()

    def wave(self) -> None:
        self.face.wave = 1.0

    # --- painting ---------------------------------------------------------------------
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setOpacity(float(self.cfg.get("look.opacity", 1.0)))
        bob = math.sin(self.face.t * 2.2) * 4 * float(self.cfg.get("move.bob", 1.0)) * self.scale()
        thrust = 0.5 + (0.5 if self.mover.hop_t < 1.0 or self.mover.gliding else 0.0)
        self.painter.draw(p, self.face, self.width(), self.height(), bob=bob,
                          squash=self.mover.squash * 0.8, lean=self.mover.lean, thrust=thrust)
        p.end()

    # --- input ------------------------------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._drag_off = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._press_pos = e.globalPosition().toPoint()
            self._dragged = False
            self.mover.target = None
            self.mover.hop_t = 1.0
            self.mover.lift = 0.0
            self.face.set_emotion("surprised", hold=0.8)

    def mouseMoveEvent(self, e) -> None:
        if self._drag_off is not None and e.buttons() & Qt.LeftButton:
            gp = e.globalPosition().toPoint()
            if (gp - self._press_pos).manhattanLength() > 5:
                self._dragged = True
            pos = gp - self._drag_off
            self.mover.pos = QPointF(pos)
            self.move(pos)
            self.moved.emit()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.LeftButton or self._drag_off is None:
            return
        self._drag_off = None
        if self._dragged:
            self.mover.squash = 1.0
            self.cfg.set("window.x", self.x())
            self.cfg.set("window.y", self.y())
            self.face.set_emotion("happy", hold=1.5)
        else:
            self.clicked.emit()

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.double_clicked.emit()

    def contextMenuEvent(self, e) -> None:
        self.menu_requested.emit(e.globalPos())

    def enterEvent(self, _e) -> None:
        self.hover_changed.emit(True)

    def leaveEvent(self, _e) -> None:
        self.hover_changed.emit(False)


# ----------------------------------------------------------------------------------------
# hover toolbar


def _icon(p: QPainter, kind: str, c: QPointF, r: float, color: QColor) -> None:
    p.setPen(QPen(color, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    x, y = c.x(), c.y()
    if kind == "screen":
        p.drawRoundedRect(QRectF(x - r * 0.6, y - r * 0.45, r * 1.2, r * 0.8), 2, 2)
        p.drawLine(QPointF(x, y + r * 0.35), QPointF(x, y + r * 0.55))
        p.drawLine(QPointF(x - r * 0.3, y + r * 0.58), QPointF(x + r * 0.3, y + r * 0.58))
    elif kind == "cam":
        p.drawRoundedRect(QRectF(x - r * 0.6, y - r * 0.35, r * 0.85, r * 0.7), 2, 2)
        path = QPainterPath()
        path.moveTo(x + r * 0.28, y - r * 0.1)
        path.lineTo(x + r * 0.6, y - r * 0.32)
        path.lineTo(x + r * 0.6, y + r * 0.32)
        path.lineTo(x + r * 0.28, y + r * 0.1)
        p.drawPath(path)
    elif kind == "mic":
        p.drawRoundedRect(QRectF(x - r * 0.2, y - r * 0.6, r * 0.4, r * 0.75), r * 0.2, r * 0.2)
        p.drawArc(QRectF(x - r * 0.4, y - r * 0.4, r * 0.8, r * 0.75), 200 * 16, 140 * 16)
        p.drawLine(QPointF(x, y + r * 0.35), QPointF(x, y + r * 0.6))
    elif kind == "chat":
        path = QPainterPath()
        path.addRoundedRect(QRectF(x - r * 0.6, y - r * 0.45, r * 1.2, r * 0.75), 4, 4)
        p.drawPath(path)
        p.drawLine(QPointF(x - r * 0.2, y + r * 0.3), QPointF(x - r * 0.4, y + r * 0.6))
    elif kind == "settings":
        p.drawEllipse(c, r * 0.25, r * 0.25)
        for i in range(8):
            a = i * math.pi / 4
            p.drawLine(QPointF(x + math.cos(a) * r * 0.38, y + math.sin(a) * r * 0.38),
                       QPointF(x + math.cos(a) * r * 0.58, y + math.sin(a) * r * 0.58))
    elif kind == "live":
        for i, h in enumerate((0.3, 0.6, 0.45, 0.2)):
            xx = x - r * 0.45 + i * r * 0.3
            p.drawLine(QPointF(xx, y - r * h), QPointF(xx, y + r * h))


class ToolBar(QWidget):
    action = Signal(str)
    hover_changed = Signal(bool)

    BUTTONS = [("screen", "Screen sight (let me see your screen)"),
               ("cam", "Camera (let me see you)"),
               ("mic", "Microphone on/off"),
               ("live", "Voice engine: Universal ↔ Gemini Live"),
               ("chat", "Chat window (type to me)"),
               ("settings", "Settings")]

    def __init__(self, cfg: Settings):
        super().__init__(None)
        self.cfg = cfg
        _setup_overlay(self, cfg.get("window.overlay_mode", "overlay"))
        self.setMouseTracking(True)
        self.states: dict[str, bool] = {}
        self.hover = -1
        self.r = 15
        self.setFixedSize(self.r * 2 + 8, len(self.BUTTONS) * (self.r * 2 + 6) + 6)

    def set_states(self, **states: bool) -> None:
        self.states.update(states)
        self.update()

    def _index_at(self, pos) -> int:
        i = int((pos.y() - 3) // (self.r * 2 + 6))
        return i if 0 <= i < len(self.BUTTONS) else -1

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        eye = QColor(self.cfg.get("look.eye_color", "#ff2a2a"))
        for i, (kind, _) in enumerate(self.BUTTONS):
            c = QPointF(self.width() / 2, 3 + self.r + i * (self.r * 2 + 6))
            on = self.states.get(kind, False)
            bg = QColor(18, 20, 24, 235) if not on else QColor(eye).darker(250)
            if i == self.hover:
                bg = bg.lighter(160)
            p.setPen(QPen(eye if on else QColor(120, 126, 136), 1.6))
            p.setBrush(bg)
            p.drawEllipse(c, self.r, self.r)
            _icon(p, kind, c, self.r, QColor(255, 235, 230) if on else QColor(200, 204, 212))
            if kind in ("mic",) and not on:
                p.setPen(QPen(QColor(255, 70, 70), 2))
                p.drawLine(QPointF(c.x() - self.r * 0.55, c.y() + self.r * 0.55),
                           QPointF(c.x() + self.r * 0.55, c.y() - self.r * 0.55))
        p.end()

    def mouseMoveEvent(self, e) -> None:
        i = self._index_at(e.position())
        if i != self.hover:
            self.hover = i
            self.update()
            if i >= 0:
                kind, tip = self.BUTTONS[i]
                if kind == "live":
                    tip += f"  (now: {self.cfg.get('engine.mode')})"
                QToolTip.showText(e.globalPosition().toPoint() + QPoint(14, 0), tip, self)

    def mousePressEvent(self, e) -> None:
        i = self._index_at(e.position())
        if i >= 0 and e.button() == Qt.LeftButton:
            self.action.emit(self.BUTTONS[i][0])

    def enterEvent(self, _e) -> None:
        self.hover_changed.emit(True)

    def leaveEvent(self, _e) -> None:
        self.hover = -1
        self.update()
        self.hover_changed.emit(False)

    def follow(self, bot: QWidget) -> None:
        g = bot.geometry()
        scr = QGuiApplication.screenAt(g.center()) or QGuiApplication.primaryScreen()
        r = scr.availableGeometry()
        x = g.right() + 2 if g.right() + self.width() + 4 < r.right() else g.left() - self.width() - 2
        y = g.top() + (g.height() - self.height()) // 2
        self.move(x, max(r.top(), y))


# ----------------------------------------------------------------------------------------
# speech bubble


class Bubble(QWidget):
    MAX_W = 340

    def __init__(self, cfg: Settings):
        super().__init__(None)
        self.cfg = cfg
        _setup_overlay(self, cfg.get("window.overlay_mode", "overlay"))
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.user = ""
        self.bot = ""
        self.tail_down = True
        self.tail_x = 40
        self.font_main = QFont()
        self.font_main.setPointSizeF(10.5)
        self.font_user = QFont()
        self.font_user.setPointSizeF(9)
        self.font_user.setItalic(True)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)
        self._bot_widget: QWidget | None = None

    def show_text(self, bot: str | None = None, user: str | None = None, final: bool = True) -> None:
        if not self.cfg.get("bot.subtitles", True):
            return
        if user is not None:
            self.user = user
        if bot is not None:
            self.bot = bot
        if len(self.bot) > 320:
            self.bot = "…" + self.bot[-320:]
        if len(self.user) > 140:
            self.user = "…" + self.user[-140:]
        self._layout()
        self.show()
        self.raise_()
        self.update()
        if final:
            secs = float(self.cfg.get("bot.subtitle_seconds", 7)) + len(self.bot) / 40
            self.hide_timer.start(int(secs * 1000))
        else:
            self.hide_timer.stop()

    def clear(self) -> None:
        self.user = ""
        self.bot = ""
        self.hide()

    def _text_rects(self) -> tuple[QRect, QRect]:
        w = self.MAX_W - 24
        fu = QFontMetrics(self.font_user)
        fm = QFontMetrics(self.font_main)
        ur = fu.boundingRect(QRect(0, 0, w, 2000), Qt.TextWordWrap, self.user) if self.user else QRect()
        br = fm.boundingRect(QRect(0, 0, w, 2000), Qt.TextWordWrap, self.bot) if self.bot else QRect()
        return ur, br

    def _layout(self) -> None:
        ur, br = self._text_rects()
        w = max(ur.width(), br.width(), 60) + 24
        h = ur.height() + br.height() + 20 + (6 if self.user and self.bot else 0) + 12
        self.setFixedSize(w, h)
        if self._bot_widget:
            self.follow(self._bot_widget)

    def follow(self, bot: QWidget) -> None:
        self._bot_widget = bot
        g = bot.geometry()
        scr = QGuiApplication.screenAt(g.center()) or QGuiApplication.primaryScreen()
        r = scr.availableGeometry()
        x = g.center().x() - self.width() // 2
        x = min(max(x, r.left() + 4), r.right() - self.width() - 4)
        y = g.top() - self.height() - 2
        self.tail_down = True
        if y < r.top():
            y = g.bottom() + 2
            self.tail_down = False
        self.tail_x = min(max(g.center().x() - x, 24), self.width() - 24)
        self.move(x, y)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        eye = QColor(self.cfg.get("look.eye_color", "#ff2a2a"))
        body = QRectF(1, 1 if self.tail_down else 13, self.width() - 2, self.height() - 14)
        path = QPainterPath()
        path.addRoundedRect(body, 12, 12)
        tail = QPainterPath()
        tx = self.tail_x
        if self.tail_down:
            tail.moveTo(tx - 9, body.bottom() - 1)
            tail.lineTo(tx, self.height() - 1)
            tail.lineTo(tx + 9, body.bottom() - 1)
        else:
            tail.moveTo(tx - 9, body.top() + 1)
            tail.lineTo(tx, 1)
            tail.lineTo(tx + 9, body.top() + 1)
        path = path.united(tail)
        p.setPen(QPen(QColor(eye).darker(130), 1.4))
        p.setBrush(QColor(20, 22, 27, 238))
        p.drawPath(path)
        ur, br = self._text_rects()
        y = body.top() + 10
        if self.user:
            p.setFont(self.font_user)
            p.setPen(QColor(170, 176, 188))
            p.drawText(QRect(12, int(y), self.width() - 24, ur.height()), Qt.TextWordWrap, self.user)
            y += ur.height() + 6
        if self.bot:
            p.setFont(self.font_main)
            p.setPen(QColor(245, 245, 248))
            p.drawText(QRect(12, int(y), self.width() - 24, br.height() + 4), Qt.TextWordWrap, self.bot)
        p.end()


# ----------------------------------------------------------------------------------------
# status pill: always says what the bot is doing or what is wrong


class StatusPill(QWidget):
    clicked = Signal()
    COLORS = {"ok": QColor(70, 200, 110), "busy": QColor(80, 160, 255), "warn": QColor(255, 190, 60),
              "error": QColor(255, 70, 70), "off": QColor(150, 150, 150)}

    def __init__(self, cfg: Settings):
        super().__init__(None)
        self.cfg = cfg
        _setup_overlay(self, cfg.get("window.overlay_mode", "overlay"))
        self.level = "off"
        self.text = ""
        self.font_ = QFont()
        self.font_.setPointSizeF(8.5)
        self.setToolTip("Status. Click for Settings → Check")

    def set_status(self, level: str, text: str) -> None:
        if (level, text) == (self.level, self.text):
            return
        self.level, self.text = level, text
        fm = QFontMetrics(self.font_)
        shown = fm.elidedText(text, Qt.ElideRight, 260)
        self._shown = shown
        self.setFixedSize(fm.horizontalAdvance(shown) + 30, fm.height() + 8)
        self.update()

    def follow(self, bot: QWidget) -> None:
        g = bot.geometry()
        scr = QGuiApplication.screenAt(g.center()) or QGuiApplication.primaryScreen()
        r = scr.availableGeometry()
        x = min(max(g.center().x() - self.width() // 2, r.left()), r.right() - self.width())
        y = g.bottom() - 4
        if y + self.height() > r.bottom():
            y = g.top() - self.height()
        self.move(x, y)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(QColor(18, 20, 24, 225))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1),
                          self.height() / 2, self.height() / 2)
        c = self.COLORS.get(self.level, self.COLORS["off"])
        if self.level == "busy":
            c.setAlphaF(0.55 + 0.45 * abs(math.sin(time.monotonic() * 3)))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(QPointF(11, self.height() / 2), 4, 4)
        p.setFont(self.font_)
        p.setPen(QColor(235, 237, 242))
        p.drawText(QRect(20, 0, self.width() - 24, self.height()), Qt.AlignVCenter, getattr(self, "_shown", ""))
        p.end()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
