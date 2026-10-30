"""Procedural chibi robot: drawing plus the animated face state (expressions, blinks, gaze).

Everything is drawn in a 200 x 250 design box and scaled to the widget, so the bot stays
sharp at any size and every expression can blend smoothly into the next."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, fields, replace

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen,
                           QRadialGradient)

DESIGN_W, DESIGN_H = 200.0, 250.0


@dataclass
class Pose:
    open_l: float = 1.0       # eyelid openness per eye (0 closed .. 1.2 wide)
    open_r: float = 1.0
    eye_scale: float = 1.0
    pupil: float = 1.0
    lid_tilt: float = 0.0     # + angry (inner corner down), - sad (outer corner down)
    lid_low: float = 0.0      # lower lid raise; 1 = happy ^ ^ eyes
    brow_tilt: float = 0.0    # + angry, - worried
    brow_raise: float = 0.0
    mouth_curve: float = 0.0  # -1 frown .. 1 smile
    arm_l: float = 0.0        # degrees, positive = raised
    arm_r: float = 0.0
    tremble: float = 0.0
    glow: float = 1.0


EXPRESSIONS: dict[str, tuple[Pose, str]] = {
    "neutral":   (Pose(), "ring"),
    "happy":     (Pose(open_l=0.95, open_r=0.95, lid_low=0.6, mouth_curve=0.8, brow_raise=0.4,
                       arm_l=15, arm_r=15), "ring"),
    "excited":   (Pose(open_l=1.0, open_r=1.0, lid_low=1.0, mouth_curve=1.0, brow_raise=0.8,
                       arm_l=80, arm_r=80, glow=1.3), "ring"),
    "love":      (Pose(mouth_curve=0.9, brow_raise=0.5, arm_l=30, arm_r=30, glow=1.2), "heart"),
    "wink":      (Pose(open_l=1.0, open_r=0.05, lid_low=0.3, mouth_curve=0.7, brow_raise=0.2,
                       arm_r=40), "ring"),
    "sad":       (Pose(open_l=0.75, open_r=0.75, lid_tilt=-0.9, brow_tilt=-0.9, mouth_curve=-0.8,
                       pupil=0.8, arm_l=-15, arm_r=-15, glow=0.7), "ring"),
    "angry":     (Pose(open_l=0.8, open_r=0.8, lid_tilt=1.0, brow_tilt=1.0, mouth_curve=-0.6,
                       pupil=0.7, arm_l=-5, arm_r=-5, glow=1.35), "ring"),
    "surprised": (Pose(open_l=1.2, open_r=1.2, eye_scale=1.15, pupil=0.6, brow_raise=1.0,
                       mouth_curve=-0.1, arm_l=45, arm_r=45), "ring"),
    "scared":    (Pose(open_l=1.15, open_r=1.15, eye_scale=0.95, pupil=0.45, brow_tilt=-0.6,
                       brow_raise=0.8, mouth_curve=-0.7, tremble=1.0, arm_l=60, arm_r=60), "ring"),
    "thinking":  (Pose(open_l=0.8, open_r=1.0, lid_tilt=0.2, brow_tilt=0.3, brow_raise=0.3,
                       mouth_curve=-0.2, arm_r=70), "spin"),
    "sleepy":    (Pose(open_l=0.2, open_r=0.2, mouth_curve=0.1, brow_raise=-0.3, glow=0.5,
                       arm_l=-10, arm_r=-10), "ring"),
    "error":     (Pose(brow_tilt=0.5, mouth_curve=-0.6, glow=0.9), "x"),
}


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class Face:
    """Animated face state. Call update(dt) every frame, then draw_robot()."""

    def __init__(self) -> None:
        self.pose = Pose()
        self.target = Pose()
        self.shape = "ring"
        self.emotion = "neutral"
        self.base_emotion = "neutral"
        self.hold = 0.0
        self.t = 0.0
        self.blink = 0.0           # 0..1 closing amount
        self._next_blink = random.uniform(2.0, 5.0)
        self.look = [0.0, 0.0]
        self.look_target = [0.0, 0.0]
        self._next_saccade = 1.0
        self.forced_look: tuple[float, float] | None = None
        self.mouth_level = 0.0     # 0..1 audio loudness (lip sync)
        self.listening = False
        self.speaking = False
        self.screen_on = False
        self.cam_on = False
        self.mic_on = True
        self.scan = -1.0           # scan line progress across the eyes, <0 = off
        self.wave = 0.0            # waving arm animation amount

    def set_emotion(self, name: str, hold: float = 0.0) -> None:
        if name not in EXPRESSIONS:
            name = "neutral"
        if hold > 0:
            self.hold = hold
        else:
            self.base_emotion = name
        self.emotion = name
        pose, shape = EXPRESSIONS[name]
        self.target = replace(pose)
        self.shape = shape

    def update(self, dt: float) -> None:
        self.t += dt
        if self.hold > 0:
            self.hold -= dt
            if self.hold <= 0:
                self.set_emotion(self.base_emotion)
        k = 1.0 - math.exp(-dt * 9.0)
        for f in fields(Pose):
            setattr(self.pose, f.name, _lerp(getattr(self.pose, f.name), getattr(self.target, f.name), k))

        # blinking
        self._next_blink -= dt
        if self._next_blink <= 0:
            self.blink += dt / 0.07
            if self.blink >= 2.0:
                self.blink = 0.0
                self._next_blink = random.uniform(2.0, 6.0) if random.random() > 0.15 else 0.2
        # gaze
        self._next_saccade -= dt
        if self.forced_look is not None:
            self.look_target = list(self.forced_look)
        elif self._next_saccade <= 0:
            self._next_saccade = random.uniform(0.8, 3.0)
            if self.emotion == "thinking":
                self.look_target = [0.6, -0.6]
            elif random.random() < 0.45:
                self.look_target = [0.0, 0.0]
            else:
                self.look_target = [random.uniform(-1, 1), random.uniform(-0.6, 0.6)]
        g = 1.0 - math.exp(-dt * 14.0)
        self.look[0] = _lerp(self.look[0], self.look_target[0], g)
        self.look[1] = _lerp(self.look[1], self.look_target[1], g)

        if self.scan >= 0:
            self.scan += dt / 0.9
            if self.scan > 1.0:
                self.scan = -1.0
        elif self.screen_on and random.random() < dt / 6.0:
            self.scan = 0.0
        self.wave = max(0.0, self.wave - dt / 2.2)

    def blink_amount(self) -> float:
        b = self.blink
        return b if b <= 1.0 else 2.0 - b


# ----------------------------------------------------------------------------------
# drawing


def _metal(tint: QColor, y0: float, y1: float) -> QLinearGradient:
    g = QLinearGradient(0, y0, 0, y1)
    light = QColor(tint).lighter(135)
    dark = QColor(tint).darker(230)
    g.setColorAt(0.0, light)
    g.setColorAt(0.35, tint)
    g.setColorAt(0.7, QColor(tint).darker(150))
    g.setColorAt(1.0, dark)
    return g


def _glow(p: QPainter, center: QPointF, radius: float, color: QColor, strength: float) -> None:
    if strength <= 0.01:
        return
    g = QRadialGradient(center, radius)
    c = QColor(color)
    c.setAlphaF(min(1.0, 0.55 * strength))
    g.setColorAt(0.0, c)
    c2 = QColor(color)
    c2.setAlphaF(min(1.0, 0.18 * strength))
    g.setColorAt(0.45, c2)
    c3 = QColor(color)
    c3.setAlpha(0)
    g.setColorAt(1.0, c3)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(center, radius, radius)


def _heart(cx: float, cy: float, r: float) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(cx, cy + r * 0.9)
    path.cubicTo(cx - r * 1.6, cy - r * 0.1, cx - r * 0.7, cy - r * 1.3, cx, cy - r * 0.45)
    path.cubicTo(cx + r * 0.7, cy - r * 1.3, cx + r * 1.6, cy - r * 0.1, cx, cy + r * 0.9)
    return path


class RobotPainter:
    def __init__(self, eye_color: str = "#ff2a2a", body_tint: str = "#c3c8d0", glow: float = 0.8):
        self.set_colors(eye_color, body_tint, glow)

    def set_colors(self, eye_color: str, body_tint: str, glow: float) -> None:
        self.eye = QColor(eye_color)
        self.tint = QColor(body_tint)
        self.glow_amt = glow
        self.socket = QColor("#16191e")
        self.line = QColor(self.tint).darker(260)

    # main entry -------------------------------------------------------------------
    def draw(self, p: QPainter, face: Face, w: float, h: float, *, bob: float = 0.0,
             squash: float = 0.0, lean: float = 0.0, thrust: float = 0.6) -> None:
        s = min(w / DESIGN_W, h / DESIGN_H)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.translate(w / 2, h / 2)
        p.scale(s, s)
        p.translate(-DESIGN_W / 2, -DESIGN_H / 2)

        pose = face.pose
        tremble = pose.tremble * math.sin(face.t * 60) * 1.2
        talk_bob = face.mouth_level * 2.5 * math.sin(face.t * 18)

        # floor/thruster glow does not bob with the body
        self._thrusters(p, face, bob, thrust)

        p.translate(100 + tremble, 235 + bob)
        p.rotate(lean)
        p.scale(1 + squash * 0.12, 1 - squash * 0.12)
        p.translate(-100, -235)

        self._legs(p)
        self._arms(p, face)
        self._body(p, face)
        p.translate(0, talk_bob)
        self._head(p, face)
        p.restore()

    # parts --------------------------------------------------------------------------
    def _thrusters(self, p: QPainter, face: Face, bob: float, thrust: float) -> None:
        flick = 0.85 + 0.15 * math.sin(face.t * 37) * math.sin(face.t * 23)
        for x in (84, 116):
            top = 222 + bob
            length = (16 + 10 * thrust) * flick
            g = QLinearGradient(0, top, 0, top + length)
            g.setColorAt(0.0, QColor(255, 230, 200, 230))
            g.setColorAt(0.25, QColor(255, 120, 60, 200))
            g.setColorAt(1.0, QColor(255, 40, 20, 0))
            path = QPainterPath()
            path.moveTo(x - 6, top)
            path.quadTo(x - 4, top + length * 0.6, x, top + length)
            path.quadTo(x + 4, top + length * 0.6, x + 6, top)
            path.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(g))
            p.drawPath(path)
            _glow(p, QPointF(x, top + 4), 16, QColor(255, 90, 40), 0.6 * self.glow_amt * flick)

    def _legs(self, p: QPainter) -> None:
        for x in (84, 116):
            p.setPen(QPen(self.line, 1.2))
            p.setBrush(QBrush(_metal(self.tint, 196, 224)))
            p.drawRoundedRect(QRectF(x - 9, 196, 18, 22), 6, 6)
            p.setBrush(QBrush(_metal(QColor(self.tint).darker(120), 214, 226)))
            p.drawRoundedRect(QRectF(x - 11, 214, 22, 10), 4, 4)
            p.setPen(QPen(QColor(self.eye).darker(110), 1.5))
            p.drawLine(QPointF(x - 5, 204), QPointF(x + 5, 204))

    def _arm(self, p: QPainter, side: int, angle: float) -> None:
        sx = 100 + side * 34
        sy = 160
        p.save()
        p.translate(sx, sy)
        p.rotate(-side * angle)
        p.setPen(QPen(self.line, 1.2))
        p.setBrush(QBrush(_metal(self.tint, -10, 40)))
        p.drawRoundedRect(QRectF(side * 4 - 8.5, -2, 17, 26), 7, 7)
        # elbow joint glow
        _glow(p, QPointF(side * 4, 24), 7, self.eye, 0.5 * self.glow_amt)
        p.setPen(QPen(self.line, 1.2))
        p.setBrush(QBrush(_metal(self.tint, 20, 50)))
        p.drawRoundedRect(QRectF(side * 5 - 7.5, 22, 15, 20), 6, 6)
        # three-finger claw hand
        p.setBrush(QBrush(QColor(self.tint).darker(130)))
        for i, dx in enumerate((-5, 0, 5)):
            p.drawRoundedRect(QRectF(side * 5 + dx - 2.4, 39, 4.8, 10 + (i == 1) * 2), 2, 2)
        p.restore()

    def _arms(self, p: QPainter, face: Face) -> None:
        wave = math.sin(face.t * 10) * 25 * face.wave
        idle = math.sin(face.t * 1.7) * 4
        self._arm(p, -1, face.pose.arm_l + idle)
        self._arm(p, 1, face.pose.arm_r - idle + max(0.0, face.wave) * 100 + wave)

    def _body(self, p: QPainter, face: Face) -> None:
        torso = QPainterPath()
        torso.moveTo(66, 150)
        torso.cubicTo(66, 142, 134, 142, 134, 150)
        torso.lineTo(126, 196)
        torso.cubicTo(120, 204, 80, 204, 74, 196)
        torso.closeSubpath()
        p.setPen(QPen(self.line, 1.4))
        p.setBrush(QBrush(_metal(self.tint, 144, 204)))
        p.drawPath(torso)
        # dark chest plate with red seams
        plate = QPainterPath()
        plate.moveTo(80, 156)
        plate.lineTo(120, 156)
        plate.lineTo(114, 190)
        plate.quadTo(100, 196, 86, 190)
        plate.closeSubpath()
        g = QLinearGradient(0, 156, 0, 194)
        g.setColorAt(0, QColor("#2b3038"))
        g.setColorAt(1, QColor("#14171b"))
        p.setBrush(QBrush(g))
        p.setPen(QPen(QColor(self.eye).darker(140), 1.0))
        p.drawPath(plate)
        # pulsing core
        pulse = 0.75 + 0.25 * math.sin(face.t * 3.0) + face.mouth_level * 0.6
        core = QPointF(100, 172)
        _glow(p, core, 16 * (0.9 + 0.2 * pulse), self.eye, pulse * self.glow_amt * face.pose.glow)
        p.setPen(QPen(QColor(self.eye).lighter(120), 2.0))
        p.setBrush(QBrush(QColor(self.eye).darker(160)))
        p.drawEllipse(core, 6.5, 6.5)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(255, 220, 200, int(200 * min(1.0, pulse)))))
        p.drawEllipse(core, 2.6, 2.6)
        # shoulder balls
        for x in (66, 134):
            rg = QRadialGradient(QPointF(x - 3, 153), 12)
            rg.setColorAt(0, QColor(self.tint).lighter(150))
            rg.setColorAt(1, QColor(self.tint).darker(200))
            p.setPen(QPen(self.line, 1.0))
            p.setBrush(QBrush(rg))
            p.drawEllipse(QPointF(x, 156), 9, 9)

    def _head_path(self) -> QPainterPath:
        head = QPainterPath()
        head.moveTo(100, 10)
        head.cubicTo(152, 10, 184, 44, 182, 90)
        head.cubicTo(180, 120, 162, 142, 134, 151)
        head.cubicTo(118, 156, 82, 156, 66, 151)
        head.cubicTo(38, 142, 20, 120, 18, 90)
        head.cubicTo(16, 44, 48, 10, 100, 10)
        return head

    def _head(self, p: QPainter, face: Face) -> None:
        head = self._head_path()
        # ears
        for side in (-1, 1):
            ex = 100 + side * 82
            p.setPen(QPen(self.line, 1.2))
            p.setBrush(QBrush(_metal(self.tint, 80, 112)))
            p.drawEllipse(QPointF(ex, 96), 11, 15)
            lvl = 0.35 + (0.65 * (0.5 + 0.5 * math.sin(face.t * 6)) if face.listening else 0)
            _glow(p, QPointF(ex, 96), 14, self.eye, lvl * self.glow_amt)
            p.setPen(QPen(QColor(self.eye), 2.0))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(ex, 96), 5, 7)

        p.setPen(QPen(self.line, 1.6))
        p.setBrush(QBrush(_metal(self.tint, 10, 156)))
        p.drawPath(head)
        # soft specular highlight on the dome
        hg = QRadialGradient(QPointF(72, 40), 60)
        hg.setColorAt(0, QColor(255, 255, 255, 150))
        hg.setColorAt(1, QColor(255, 255, 255, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(hg))
        p.save()
        p.setClipPath(head)
        p.drawEllipse(QPointF(72, 40), 60, 46)
        p.restore()

        # sculpted face mask: slightly darker plate around eyes and mouth
        mask = QPainterPath()
        mask.moveTo(46, 74)
        mask.cubicTo(70, 66, 88, 70, 100, 80)
        mask.cubicTo(112, 70, 130, 66, 154, 74)
        mask.cubicTo(166, 100, 158, 128, 134, 146)
        mask.cubicTo(118, 152, 82, 152, 66, 146)
        mask.cubicTo(42, 128, 34, 100, 46, 74)
        mg = QLinearGradient(0, 70, 0, 150)
        mg.setColorAt(0, QColor(self.tint).darker(118))
        mg.setColorAt(1, QColor(self.tint).darker(175))
        p.setPen(QPen(QColor(self.line), 1.2))
        p.setBrush(QBrush(mg))
        p.drawPath(mask)
        ridge = QPainterPath()
        ridge.moveTo(100, 82)
        ridge.lineTo(100, 122)
        p.setPen(QPen(QColor(self.tint).lighter(115), 1.6))
        p.drawPath(ridge)

        # panel lines: crest, temples, cheeks
        pen = QPen(QColor(self.line), 1.3)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        crest = QPainterPath()
        crest.moveTo(100, 12)
        crest.lineTo(100, 58)
        p.drawPath(crest)
        for side in (-1, 1):
            pth = QPainterPath()
            pth.moveTo(100 + side * 10, 14)
            pth.cubicTo(100 + side * 22, 34, 100 + side * 20, 50, 100 + side * 12, 64)
            p.drawPath(pth)
            temple = QPainterPath()
            temple.moveTo(100 + side * 52, 22)
            temple.cubicTo(100 + side * 70, 40, 100 + side * 76, 60, 100 + side * 78, 74)
            p.drawPath(temple)
            cheek = QPainterPath()
            cheek.moveTo(100 + side * 58, 122)
            cheek.cubicTo(100 + side * 48, 134, 100 + side * 34, 142, 100 + side * 22, 146)
            p.drawPath(cheek)
        # red seam glow along the temples
        seam = QPen(QColor(self.eye).darker(115), 1.0)
        p.setPen(seam)
        for side in (-1, 1):
            p.drawLine(QPointF(100 + side * 64, 66), QPointF(100 + side * 72, 80))

        self._brows(p, face)
        for side in (-1, 1):
            self._eye(p, face, side)
        self._mouth(p, face)
        self._badges(p, face)

    def _brows(self, p: QPainter, face: Face) -> None:
        pose = face.pose
        for side in (-1, 1):
            cx = 100 + side * 33
            cy = 66 - pose.brow_raise * 5
            inner = -side  # inner corner points toward the center line
            p.save()
            p.translate(cx, cy)
            p.rotate(pose.brow_tilt * 16 * -inner)
            p.setPen(QPen(self.line, 1.2))
            p.setBrush(QBrush(_metal(QColor(self.tint).lighter(108), -5, 5)))
            path = QPainterPath()
            path.moveTo(-22, 2)
            path.quadTo(0, -7, 22, 2)
            path.lineTo(20, 6)
            path.quadTo(0, -1, -20, 6)
            path.closeSubpath()
            p.drawPath(path)
            p.restore()

    def _eye(self, p: QPainter, face: Face, side: int) -> None:
        pose = face.pose
        cx, cy = 100 + side * 33, 97
        r = 17 * pose.eye_scale
        socket = QPainterPath()
        socket.addEllipse(QPointF(cx, cy), 27, 25)
        sg = QRadialGradient(QPointF(cx, cy - 4), 30)
        sg.setColorAt(0, QColor("#262a31"))
        sg.setColorAt(1, self.socket)
        p.setPen(QPen(QColor(self.tint).darker(170), 2.0))
        p.setBrush(QBrush(sg))
        p.drawPath(socket)

        openness = (pose.open_l if side < 0 else pose.open_r)
        openness *= 1.0 - face.blink_amount()
        lx, ly = face.look[0] * 5.5, face.look[1] * 4.5
        ecx, ecy = cx + lx, cy + ly
        strength = self.glow_amt * pose.glow * max(0.15, min(1.0, openness))

        p.save()
        p.setClipPath(socket)
        _glow(p, QPointF(ecx, ecy), r * 1.9, self.eye, strength)
        if face.shape == "heart":
            p.setPen(QPen(QColor(self.eye).lighter(130), 2.0))
            p.setBrush(QBrush(self.eye))
            p.drawPath(_heart(ecx, ecy, r * 0.95))
        elif face.shape == "x":
            p.setPen(QPen(self.eye, 4.5, Qt.SolidLine, Qt.RoundCap))
            d = r * 0.7
            p.drawLine(QPointF(ecx - d, ecy - d), QPointF(ecx + d, ecy + d))
            p.drawLine(QPointF(ecx - d, ecy + d), QPointF(ecx + d, ecy - d))
        else:
            self._ring_eye(p, face, ecx, ecy, r)
        if face.scan >= 0:
            y = cy - 25 + 50 * face.scan
            c = QColor(255, 255, 255, 140)
            p.setPen(QPen(c, 1.5))
            p.drawLine(QPointF(cx - 27, y), QPointF(cx + 27, y))
        if face.shape not in ("heart", "x"):
            self._lids(p, face, cx, cy, r, openness, side)
        p.restore()

    def _ring_eye(self, p: QPainter, face: Face, cx: float, cy: float, r: float) -> None:
        pose = face.pose
        pulse = 1.0 + (0.05 * math.sin(face.t * 8) if face.listening else 0.0)
        rr = r * pulse
        inner = QRadialGradient(QPointF(cx, cy), rr)
        inner.setColorAt(0, QColor(self.eye).darker(120))
        inner.setColorAt(1, QColor(self.eye).darker(260))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(inner))
        p.drawEllipse(QPointF(cx, cy), rr, rr)
        ring_pen = QPen(QColor(self.eye).lighter(115), rr * 0.24)
        if face.shape == "spin":
            ring_pen.setCapStyle(Qt.RoundCap)
            p.setPen(ring_pen)
            start = int(-face.t * 360 * 1.4 * 16) % (360 * 16)
            p.drawArc(QRectF(cx - rr * 0.82, cy - rr * 0.82, rr * 1.64, rr * 1.64), start, 250 * 16)
        else:
            p.setPen(ring_pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), rr * 0.82, rr * 0.82)
        # bright core
        core_r = rr * 0.36 * pose.pupil
        cg = QRadialGradient(QPointF(cx, cy), core_r * 1.6)
        cg.setColorAt(0, QColor(255, 245, 230))
        cg.setColorAt(0.5, QColor(self.eye).lighter(160))
        cg.setColorAt(1, QColor(self.eye).lighter(110))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(cg))
        p.drawEllipse(QPointF(cx, cy), core_r, core_r)
        p.setBrush(QBrush(QColor(255, 255, 255, 200)))
        p.drawEllipse(QPointF(cx - rr * 0.42, cy - rr * 0.45), rr * 0.13, rr * 0.13)

    def _lids(self, p: QPainter, face: Face, cx: float, cy: float, r: float,
              openness: float, side: int) -> None:
        pose = face.pose
        inner_dir = -side  # +x for the left eye, -x for the right eye
        lid_col = QColor("#1b1f25")
        edge = QPen(QColor(self.tint).darker(140), 1.6)
        # upper lid: everything above a tilted line
        top = cy + r * 1.15 - 2 * r * 1.15 * openness

        def y_at(x: float) -> float:
            return top + pose.lid_tilt * (x - cx) * inner_dir * 0.55

        x0, x1 = cx - 30, cx + 30
        upper = QPainterPath()
        upper.moveTo(x0, y_at(x0))
        upper.lineTo(x1, y_at(x1))
        upper.lineTo(x1, cy - 40)
        upper.lineTo(x0, cy - 40)
        upper.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(lid_col))
        p.drawPath(upper)
        p.setPen(edge)
        p.drawLine(QPointF(x0, y_at(x0)), QPointF(x1, y_at(x1)))
        # lower lid: rising arc for happy squints
        if pose.lid_low > 0.02:
            base = cy + r * 1.2
            peak = base - pose.lid_low * r * 1.75
            lower = QPainterPath()
            lower.moveTo(x0, base)
            lower.quadTo(cx, peak - (base - peak) * 0.6, x1, base)
            lower.lineTo(x1, cy + 40)
            lower.lineTo(x0, cy + 40)
            lower.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(lid_col))
            p.drawPath(lower)
        if openness < 0.12:
            # closed: a thin glowing line
            p.setPen(QPen(QColor(self.eye), 2.4, Qt.SolidLine, Qt.RoundCap))
            y = cy + 2
            p.drawLine(QPointF(cx - r * 0.8, y), QPointF(cx + r * 0.8, y))

    def _mouth(self, p: QPainter, face: Face) -> None:
        pose = face.pose
        cx, cy = 100, 134
        w, h = 40, 15
        box = QRectF(cx - w / 2, cy - h / 2, w, h)
        p.setPen(QPen(QColor(self.tint).darker(170), 1.5))
        g = QLinearGradient(0, box.top(), 0, box.bottom())
        g.setColorAt(0, QColor("#22262c"))
        g.setColorAt(1, QColor("#0f1114"))
        p.setBrush(QBrush(g))
        curve = pose.mouth_curve
        path = QPainterPath()
        path.moveTo(box.left(), box.top() - curve * 3)
        path.quadTo(cx, box.top() + curve * 6, box.right(), box.top() - curve * 3)
        path.lineTo(box.right() - 3, box.bottom() - curve * 3)
        path.quadTo(cx, box.bottom() + curve * 6 + face.mouth_level * 5, box.left() + 3,
                    box.bottom() - curve * 3)
        path.closeSubpath()
        p.drawPath(path)
        lvl = face.mouth_level if face.mic_on or face.speaking else 0.0
        for i in range(3):
            y = box.top() + 4 + i * 3.6 + face.mouth_level * i * 1.2
            alpha = int(60 + 195 * min(1.0, lvl * (1.3 - i * 0.2)))
            c = QColor(self.eye)
            c.setAlpha(alpha)
            p.setPen(QPen(c, 1.6, Qt.SolidLine, Qt.RoundCap))
            slat = QPainterPath()
            slat.moveTo(box.left() + 5, y - curve * 2.5)
            slat.quadTo(cx, y + curve * 4, box.right() - 5, y - curve * 2.5)
            p.setBrush(Qt.NoBrush)
            p.drawPath(slat)
        if face.mouth_level > 0.05:
            _glow(p, QPointF(cx, cy), 22, self.eye, face.mouth_level * 0.8 * self.glow_amt)

    def _badges(self, p: QPainter, face: Face) -> None:
        blink_on = int(face.t * 2) % 2 == 0
        if face.cam_on:
            c = QColor(255, 40, 40, 255 if blink_on else 90)
            _glow(p, QPointF(100, 30), 8, c, 0.8)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(c))
            p.drawEllipse(QPointF(100, 30), 3, 3)
        if face.screen_on:
            p.setPen(QPen(QColor(self.eye).lighter(120), 1.4))
            p.setBrush(QBrush(QColor(20, 22, 26, 220)))
            p.drawRoundedRect(QRectF(140, 28, 16, 11), 2, 2)
            p.drawLine(QPointF(148, 39), QPointF(148, 42))
            p.drawLine(QPointF(144, 42), QPointF(152, 42))
        if not face.mic_on:
            p.setPen(QPen(QColor(230, 230, 230), 1.4))
            p.setBrush(QBrush(QColor(20, 22, 26, 220)))
            p.drawRoundedRect(QRectF(44, 28, 9, 13), 4, 4)
            p.setPen(QPen(QColor(255, 60, 60), 2.0))
            p.drawLine(QPointF(41, 43), QPointF(56, 26))
