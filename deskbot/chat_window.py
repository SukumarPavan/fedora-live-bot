"""A normal window to type to the bot and read the conversation log."""
from __future__ import annotations

import html
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser,
                               QVBoxLayout, QWidget)


class ChatWindow(QWidget):
    send_text = Signal(str)
    clear_memory = Signal()

    def __init__(self, bot_name: str):
        super().__init__(None, Qt.Window)
        self.setWindowTitle(f"Chat with {bot_name}")
        self.resize(460, 560)
        self.bot_name = bot_name
        self.log = QTextBrowser()
        self.log.setOpenExternalLinks(True)
        self.live = QLabel("")
        self.live.setWordWrap(True)
        self.live.setStyleSheet("color: #888; font-style: italic;")
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a message and press Enter")
        self.input.returnPressed.connect(self._send)
        send = QPushButton("Send")
        send.clicked.connect(self._send)
        forget = QPushButton("Forget conversation")
        forget.setToolTip("Clear the bot's short-term memory")
        forget.clicked.connect(self.clear_memory.emit)
        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(send)
        lay = QVBoxLayout(self)
        lay.addWidget(self.log, 1)
        lay.addWidget(self.live)
        lay.addLayout(row)
        lay.addWidget(forget, 0, Qt.AlignRight)

    def _send(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.send_text.emit(text)

    def _add(self, who: str, text: str, color: str) -> None:
        ts = time.strftime("%H:%M")
        self.log.append(f'<p><span style="color:#999">{ts}</span> <b style="color:{color}">'
                        f'{html.escape(who)}:</b> {html.escape(text)}</p>')

    def add_user(self, text: str) -> None:
        self.live.setText("")
        self._add("You", text, "#4a90d9")

    def add_bot(self, text: str, final: bool) -> None:
        if final:
            self.live.setText("")
            self._add(self.bot_name, text, "#e0403a")
        else:
            self.live.setText(f"{self.bot_name}: {text}")

    def add_notice(self, text: str) -> None:
        self._add("note", text, "#c08a00")
