"""Render every expression to a PNG sheet (and the app icon) without a display."""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QColor, QFont
from PySide6.QtCore import Qt, QRectF
from deskbot.art import Face, RobotPainter, EXPRESSIONS

app = QGuiApplication([])
names = list(EXPRESSIONS)
cw, ch = 220, 290
cols = 4
rows = (len(names) + cols - 1) // cols
img = QImage(cw * cols, ch * rows, QImage.Format_ARGB32)
img.fill(QColor("#3a4a5e"))
p = QPainter(img)
rp = RobotPainter()
for i, n in enumerate(names):
    f = Face(); f.set_emotion(n)
    f.t = 0.4
    for _ in range(60): f.update(1/60)
    f.blink = 0; f.look = [0, 0]
    if n == "happy": f.mouth_level = 0.6; f.speaking = True
    if n == "neutral": f.screen_on = True; f.cam_on = True
    if n == "thinking": f.mic_on = False
    p.save(); p.translate((i % cols) * cw, (i // cols) * ch)
    rp.draw(p, f, cw, ch - 30)
    p.setPen(QColor("white")); p.setFont(QFont("Sans", 11))
    p.drawText(QRectF(0, ch - 30, cw, 26), Qt.AlignCenter, n)
    p.restore()
p.end()
out = sys.argv[1] if len(sys.argv) > 1 else "sheet.png"
img.save(out)
# icon
icon = QImage(256, 256, QImage.Format_ARGB32); icon.fill(Qt.transparent)
p = QPainter(icon); f = Face(); f.set_emotion("happy")
for _ in range(60): f.update(1/60)
f.blink = 0; f.look = [0, 0]
p.translate(-20, 0)
rp.draw(p, f, 296, 370)
p.end()
icon.save(sys.argv[2] if len(sys.argv) > 2 else "icon.png")
print("ok")
