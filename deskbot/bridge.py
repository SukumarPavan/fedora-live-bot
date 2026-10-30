"""Thread-safe signals from the engines (asyncio thread) to the Qt UI."""
from PySide6.QtCore import QObject, Signal


class Bridge(QObject):
    state = Signal(str)            # idle | listening | thinking | speaking
    emotion = Signal(str, float)   # name, hold seconds (0 = becomes the base mood)
    user_text = Signal(str)        # what the bot heard (may update while you talk)
    bot_text = Signal(str, bool)   # what the bot is saying, final?
    notice = Signal(str)           # errors and info for the bubble and chat log
    eyes_failed = Signal(str)      # "screen" or "cam" failed and was switched off
    status = Signal(str, str, str)  # part (engine|mic|screen|cam), level (ok|busy|warn|error|off), text
