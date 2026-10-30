"""Screen-share D-Bus flow against a fake xdg-desktop-portal on a private bus."""
import os
import shutil
import subprocess
import threading

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("dbus-daemon"), reason="dbus-daemon not installed")

REQ = "org.freedesktop.portal.Request"


def fake_portal(address: str, answer_start: bool, ready: threading.Event, stop: threading.Event):
    from jeepney import DBusAddress, new_method_return, new_signal
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import Proxy, open_dbus_connection
    from jeepney.low_level import HeaderFields, MessageType

    conn = open_dbus_connection(bus=address, enable_fds=True)
    Proxy(message_bus, conn).RequestName("org.freedesktop.portal.Desktop")
    ready.set()
    r, w = os.pipe()

    def respond(msg, token, results):
        sender = msg.header.fields[HeaderFields.sender][1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        conn.send(new_method_return(msg, "o", (path,)))
        conn.send(new_signal(DBusAddress(path, interface=REQ), "Response", "ua{sv}", (0, results)))

    while not stop.is_set():
        try:
            msg = conn.receive(timeout=0.2)
        except TimeoutError:
            continue
        except OSError:  # bus shut down at test teardown
            break
        if msg.header.message_type != MessageType.method_call:
            continue
        member = msg.header.fields[HeaderFields.member]
        if member == "Get":
            val = {"version": ("u", 5), "AvailableCursorModes": ("u", 7)}[msg.body[1]]
            conn.send(new_method_return(msg, "v", (val,)))
        elif member == "CreateSession":
            opts = msg.body[0]
            respond(msg, opts["handle_token"][1], {"session_handle": (
                "s", "/org/freedesktop/portal/desktop/session/x/" + opts["session_handle_token"][1])})
        elif member == "SelectSources":
            respond(msg, msg.body[1]["handle_token"][1], {})
        elif member == "Start":
            if answer_start:
                respond(msg, msg.body[2]["handle_token"][1],
                        {"streams": ("a(ua{sv})", [(42, {})]), "restore_token": ("s", "tok123")})
            else:  # the user never clicks Share
                sender = msg.header.fields[HeaderFields.sender][1:].replace(".", "_")
                conn.send(new_method_return(msg, "o", (
                    f"/org/freedesktop/portal/desktop/request/{sender}/{msg.body[2]['handle_token'][1]}",)))
        elif member == "OpenPipeWireRemote":
            conn.send(new_method_return(msg, "h", (r,)))
        elif member == "Close":
            conn.send(new_method_return(msg, "", ()))
    conn.close()


@pytest.fixture
def bus():
    proc = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1", "--nopidfile"],
                            stdout=subprocess.PIPE, text=True)
    address = proc.stdout.readline().strip()
    old = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
    yield address
    proc.terminate()
    if old is None:
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
    else:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = old


def run_portal(address, answer_start=True):
    ready, stop = threading.Event(), threading.Event()
    t = threading.Thread(target=fake_portal, args=(address, answer_start, ready, stop), daemon=True)
    t.start()
    assert ready.wait(5)
    return stop


def test_full_screencast_handshake(bus):
    from deskbot.portal import PortalSession
    stop = run_portal(bus)
    waited = []
    s = PortalSession()
    node, fd, token = s.start("", on_wait=lambda: waited.append(True))
    s.close()
    stop.set()
    assert (node, token, waited) == (42, "tok123", [True])
    assert fd >= 0
    os.close(fd)


def test_not_picking_a_screen_gives_a_clear_error(bus, monkeypatch):
    from deskbot import portal
    monkeypatch.setattr(portal, "START_TIMEOUT_S", 1.0)
    stop = run_portal(bus, answer_start=False)
    with pytest.raises(portal.PortalError, match="didn't pick a screen"):
        portal.PortalSession().start("")
    stop.set()


def test_switching_off_during_dialog_does_not_crash(bus, monkeypatch):
    from deskbot import portal
    monkeypatch.setattr(portal, "START_TIMEOUT_S", 1.0)
    stop = run_portal(bus, answer_start=False)
    cap = portal.PortalCapture(lambda f: None, lambda m: None, lambda t: None)
    monkeypatch.setattr(portal.shutil, "which", lambda name: "/usr/bin/" + name)
    errs = []
    th = threading.Thread(target=lambda: errs.append(pytest.raises(portal.PortalError, cap.start,
                                                                   "", "x", 640, 1, 70)))
    th.start()
    cap.stop()  # user turns Screen sight off while GNOME's dialog is open
    th.join(5)
    stop.set()
    assert errs  # raised the friendly PortalError, not AttributeError
