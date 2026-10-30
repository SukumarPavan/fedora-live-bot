"""Entry point: `deskbot` (app), `deskbot --selftest [--screen]`, `deskbot --reset-position`."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys

from . import __version__
from .config import APP_ID, APP_NAME, ASSETS_DIR, LOG_FILE, Settings


def _setup_logging(level: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    for noisy in ("httpx", "httpcore", "websockets", "google_genai", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP_ID, description=f"{APP_NAME} desktop companion")
    ap.add_argument("--selftest", action="store_true", help="check this computer and exit")
    ap.add_argument("--screen", action="store_true", help="with --selftest: also capture one screen frame")
    ap.add_argument("--reset-position", action="store_true", help="bring the bot back to the corner")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    cfg = Settings().load()
    _setup_logging(cfg.get("system.log_level", "INFO"))
    log = logging.getLogger(APP_ID)

    if args.selftest:
        from .selftest import run
        return run(cfg, screen=args.screen)
    if args.reset_position:
        cfg.set("window.x", None, save=False)
        cfg.set("window.y", None, save=False)
        cfg.save()

    # GNOME/KDE Wayland don't let apps place windows or keep them on top: use XWayland.
    if (sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY")
            and cfg.get("window.force_xwayland", True) and "QT_QPA_PLATFORM" not in os.environ):
        os.environ["QT_QPA_PLATFORM"] = "xcb"

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QCoreApplication.setApplicationName(APP_NAME)
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setDesktopFileName(APP_ID)
    icon = ASSETS_DIR / "icon.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    # single instance: a second launch opens Settings in the running bot
    server_name = f"{APP_ID}-{os.getuid() if hasattr(os, 'getuid') else 'user'}"
    sock = QLocalSocket()
    sock.connectToServer(server_name)
    if sock.waitForConnected(300):
        sock.write(b"settings")
        sock.waitForBytesWritten(300)
        print("DeskBot is already running; opened its Settings.")
        return 0
    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    server.listen(server_name)

    from .controller import Controller
    ctl = Controller(app, cfg)

    def on_conn():
        c = server.nextPendingConnection()
        if c:
            c.waitForReadyRead(300)
            ctl.open_settings()
    server.newConnection.connect(on_conn)

    log.info("%s %s starting (platform plugin: %s)", APP_NAME, __version__, app.platformName())
    if app.platformName() == "wayland":
        log.warning("Running as a native Wayland client: the bot cannot move itself or stay on top")
    ctl.start()

    import signal
    signal.signal(signal.SIGINT, lambda *_: ctl.quit())
    signal.signal(signal.SIGTERM, lambda *_: ctl.quit())
    from PySide6.QtCore import QTimer
    keepalive = QTimer()
    keepalive.start(500)  # lets Python handle Ctrl+C while Qt runs
    keepalive.timeout.connect(lambda: None)
    return app.exec()
