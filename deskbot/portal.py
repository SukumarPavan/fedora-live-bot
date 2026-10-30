"""Live screen capture on Wayland through xdg-desktop-portal ScreenCast + PipeWire.

The portal hands us a PipeWire node and file descriptor; `gst-launch-1.0 pipewiresrc`
turns it into a stream of JPEG frames on stdout. GNOME shows its monitor picker the
first time only: the restore token returned by Start() is saved and reused."""
from __future__ import annotations

import logging
import secrets
import shlex
import shutil
import subprocess
import threading
from typing import Callable

log = logging.getLogger(__name__)

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.ScreenCast"


class PortalError(RuntimeError):
    pass


class PortalSession:
    def __init__(self):
        from jeepney import DBusAddress
        from jeepney.io.blocking import open_dbus_connection
        self.conn = open_dbus_connection(bus="SESSION", enable_fds=True)
        self.addr = DBusAddress(PATH, bus_name=BUS, interface=IFACE)
        self.sender = self.conn.unique_name[1:].replace(".", "_")
        self.session: str | None = None

    def _version(self) -> int:
        from jeepney import Properties
        from jeepney.wrappers import unwrap_msg
        try:
            reply = self.conn.send_and_get_reply(Properties(self.addr).get("version"), timeout=5)
            return int(unwrap_msg(reply)[0][1])
        except Exception:
            return 0

    def _cursor_modes(self) -> int:
        from jeepney import Properties
        from jeepney.wrappers import unwrap_msg
        try:
            reply = self.conn.send_and_get_reply(Properties(self.addr).get("AvailableCursorModes"), timeout=5)
            return int(unwrap_msg(reply)[0][1])
        except Exception:
            return 0

    def _request(self, method: str, signature: str, args: tuple, token: str, timeout: float = 180):
        from jeepney import MatchRule, new_method_call
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import Proxy
        from jeepney.wrappers import unwrap_msg
        req_path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                         member="Response", path=req_path)
        Proxy(message_bus, self.conn).AddMatch(rule)
        with self.conn.filter(rule) as queue:
            reply = self.conn.send_and_get_reply(new_method_call(self.addr, method, signature, args),
                                                 timeout=30)
            unwrap_msg(reply)
            msg = self.conn.recv_until_filtered(queue, timeout=timeout)
        code, results = msg.body
        if code == 1:
            raise PortalError("Screen sharing was cancelled.")
        if code != 0:
            raise PortalError(f"Screen sharing failed (portal code {code}).")
        return results

    def start(self, restore_token: str = "") -> tuple[int, int, str]:
        """Returns (pipewire_node_id, pipewire_fd, new_restore_token)."""
        from jeepney import new_method_call
        from jeepney.wrappers import unwrap_msg
        version = self._version()
        if version == 0:
            raise PortalError("xdg-desktop-portal ScreenCast is not available.")
        t1 = "deskbot" + secrets.token_hex(4)
        res = self._request("CreateSession", "a{sv}", ({
            "handle_token": ("s", t1), "session_handle_token": ("s", "s" + t1)},), t1)
        self.session = res["session_handle"][1]

        t2 = "deskbot" + secrets.token_hex(4)
        opts = {"handle_token": ("s", t2), "types": ("u", 1), "multiple": ("b", False)}
        if self._cursor_modes() & 2:
            opts["cursor_mode"] = ("u", 2)  # embedded cursor: the bot sees what you point at
        if version >= 4:
            opts["persist_mode"] = ("u", 2)
            if restore_token:
                opts["restore_token"] = ("s", restore_token)
        self._request("SelectSources", "oa{sv}", (self.session, opts), t2)

        t3 = "deskbot" + secrets.token_hex(4)
        res = self._request("Start", "osa{sv}", (self.session, "", {"handle_token": ("s", t3)}), t3)
        streams = res.get("streams", (None, []))[1]
        if not streams:
            raise PortalError("No screen was selected.")
        node_id = int(streams[0][0])
        new_token = res.get("restore_token", ("s", ""))[1]

        reply = self.conn.send_and_get_reply(
            new_method_call(self.addr, "OpenPipeWireRemote", "oa{sv}", (self.session, {})), timeout=10)
        fd_obj = unwrap_msg(reply)[0]
        fd = fd_obj.to_raw_fd() if hasattr(fd_obj, "to_raw_fd") else int(fd_obj)
        return node_id, fd, new_token

    def close(self) -> None:
        from jeepney import DBusAddress, new_method_call
        try:
            if self.session:
                addr = DBusAddress(self.session, bus_name=BUS, interface="org.freedesktop.portal.Session")
                self.conn.send(new_method_call(addr, "Close"))
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass


class JpegStreamSplitter:
    """Splits a byte stream of concatenated JPEG images (as written by jpegenc ! fdsink)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        frames = []
        while True:
            start = self.buf.find(b"\xff\xd8")
            if start < 0:
                self.buf.clear()
                break
            end = self.buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start:
                    del self.buf[:start]
                break
            frames.append(bytes(self.buf[start:end + 2]))
            del self.buf[:end + 2]
        return frames


class PortalCapture:
    def __init__(self, on_frame: Callable[[bytes], None], on_error: Callable[[str], None],
                 save_token: Callable[[str], None]):
        self.on_frame = on_frame
        self.on_error = on_error
        self.save_token = save_token
        self.session: PortalSession | None = None
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()

    def start(self, restore_token: str, template: str, width: int, fps: float, quality: int) -> None:
        if not shutil.which("gst-launch-1.0"):
            raise PortalError("gst-launch-1.0 missing. Run: sudo dnf install gstreamer1 pipewire-gstreamer "
                              "gstreamer1-plugins-good")
        self._stop.clear()
        self.session = PortalSession()
        try:
            node, fd, token = self.session.start(restore_token)
        except Exception:
            self.session.close()
            self.session = None
            raise
        if token:
            self.save_token(token)
        pipeline = template.format(fd=fd, node=node, width=int(width), quality=int(quality),
                                   fps=max(1, int(round(fps))))
        cmd = ["gst-launch-1.0", "-q"] + shlex.split(pipeline)
        log.info("Starting screen pipeline: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     pass_fds=(fd,), bufsize=0)
        import os
        os.close(fd)
        threading.Thread(target=self._read, daemon=True, name="portal-reader").start()

    def _read(self) -> None:
        split = JpegStreamSplitter()
        proc = self.proc
        assert proc and proc.stdout
        while not self._stop.is_set():
            data = proc.stdout.read(65536)
            if not data:
                break
            frames = split.feed(data)
            if frames:
                self.on_frame(frames[-1])
        if not self._stop.is_set():
            err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")[-400:]
            log.error("Screen pipeline ended: %s", err)
            self.on_error("Screen capture stopped. " + (err.strip().splitlines() or [""])[-1])

    def stop(self) -> None:
        self._stop.set()
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        if self.session:
            self.session.close()
            self.session = None
