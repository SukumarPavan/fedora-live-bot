"""`deskbot --selftest`: checks everything the bot needs on this computer and says how to fix it."""
from __future__ import annotations

import asyncio
import ctypes.util
import os
import platform
import shutil
import subprocess
import sys

from .config import PROVIDERS, VAD_MODEL, Settings

OK, WARN, FAIL = "  OK  ", " WARN ", " FAIL "


def _line(status: str, what: str, hint: str = "") -> None:
    print(f"[{status}] {what}" + (f"\n         → {hint}" if hint else ""))


def run(cfg: Settings, quick: bool = False, screen: bool = False) -> int:
    fails = 0
    print(f"DeskBot self-test on {platform.platform()} (Python {platform.python_version()})")
    st = os.environ.get("XDG_SESSION_TYPE", "?")
    de = os.environ.get("XDG_CURRENT_DESKTOP", "?")
    _line(OK, f"Session: {st} / desktop: {de} / QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM', '')}")

    if sys.platform.startswith("linux"):
        if ctypes.util.find_library("xcb-cursor"):
            _line(OK, "libxcb-cursor present (needed by Qt under XWayland)")
        else:
            fails += 1
            _line(FAIL, "libxcb-cursor missing", "sudo dnf install xcb-util-cursor")

    # audio
    try:
        import sounddevice as sd
        ins = [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
        outs = [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0]
        _line(OK if ins else FAIL, f"Microphones: {len(ins)}  Speakers: {len(outs)}",
              "" if ins else "No microphone found")
        fails += 0 if ins else 1
    except Exception as e:
        fails += 1
        _line(FAIL, f"Audio (PortAudio): {e}", "sudo dnf install portaudio")

    if sys.platform.startswith("linux") and shutil.which("pactl"):
        try:
            src = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True,
                                 timeout=5).stdout.strip()
            if "deskbot_ec" in src:
                _line(OK, f"Echo cancellation active (default mic: {src})")
            else:
                _line(WARN, f"Default mic is '{src}', not echo-cancelled",
                      "With speakers the bot may hear itself. Re-run install.sh and enable echo cancel, "
                      "use headphones, or raise 'Interrupt needs voice confidence'.")
        except Exception:
            pass

    try:
        from .vad import SileroVAD
        SileroVAD(VAD_MODEL)
        _line(OK, "Speech detector (Silero VAD) loads")
    except Exception as e:
        _line(WARN, f"Silero VAD failed ({e}); energy fallback will be used")

    for mod, hint in (("miniaudio", "pip install miniaudio"), ("edge_tts", "pip install edge-tts"),
                      ("cv2", "pip install opencv-python-headless"), ("google.genai", "pip install google-genai"),
                      ("openai", "pip install openai")):
        try:
            __import__(mod)
            _line(OK, f"Python module {mod}")
        except Exception as e:
            fails += 1
            _line(FAIL, f"Python module {mod}: {e}", hint)

    # screen
    from .vision import session_type
    if session_type() == "wayland":
        gst = shutil.which("gst-launch-1.0")
        _line(OK if gst else FAIL, "gst-launch-1.0" + (" found" if gst else " missing"),
              "" if gst else "sudo dnf install gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good")
        fails += 0 if gst else 1
        if shutil.which("gst-inspect-1.0"):
            r = subprocess.run(["gst-inspect-1.0", "pipewiresrc"], capture_output=True, timeout=10)
            _line(OK if r.returncode == 0 else FAIL, "GStreamer pipewiresrc plugin",
                  "" if r.returncode == 0 else "sudo dnf install pipewire-gstreamer")
            fails += 0 if r.returncode == 0 else 1
        try:
            from .portal import PortalSession
            s = PortalSession()
            v = s._version()
            s.close()
            _line(OK if v else FAIL, f"Screen-cast portal version {v}",
                  "" if v else "Install xdg-desktop-portal-gnome (or -kde)")
        except Exception as e:
            fails += 1
            _line(FAIL, f"D-Bus portal: {e}")
    else:
        try:
            import mss
            with mss.mss() as s:
                _line(OK, f"Direct screen grab: {len(s.monitors) - 1} monitor(s)")
        except Exception as e:
            _line(WARN, f"Direct screen grab: {e}")

    if screen:
        _screen_test(cfg)

    # camera
    if not quick:
        from .vision import detect_cameras
        cams = detect_cameras(4)
        _line(OK if cams else WARN, f"Cameras: {cams or 'none found'}",
              "" if cams else "Laptop IPU6/MIPI cameras may need extra drivers (libcamera / v4l2loopback)")

    # API keys
    from . import llm
    for role, pkey in (("Brain", "brain.provider"), ("Hearing", "ears.provider")):
        provider = cfg.get(pkey)
        label = PROVIDERS.get(provider, {}).get("label", provider)
        if not cfg.key(provider):
            fails += 1
            _line(FAIL, f"{role}: no API key for {label}", "Settings → Brain (AI) → API keys")
            continue
        try:
            models = asyncio.run(llm.list_models(cfg, provider))
            want = cfg.get("brain.model" if role == "Brain" else "ears.model")
            found = want in models
            _line(OK if found else WARN, f"{role}: {label} key works ({len(models)} models)"
                  + ("" if found else f"; model '{want}' not in list"),
                  "" if found else "Settings → pick a model with Fetch models")
        except Exception as e:
            fails += 1
            _line(FAIL, f"{role}: {label}: {e}")
    if cfg.get("engine.mode") == "live" and not cfg.key("gemini"):
        fails += 1
        _line(FAIL, "Live mode selected but no Gemini key")

    if not quick and cfg.get("voice.engine") == "edge":
        try:
            from .tts import TTS
            pcm = asyncio.run(TTS(cfg)._edge("Test."))
            _line(OK, f"Edge voice works ({len(pcm) / 24000:.1f}s of audio)")
        except Exception as e:
            _line(WARN, f"Edge voice failed: {e}", "Choose another voice engine in Settings → Voice")

    print(f"\n{'All good!' if not fails else f'{fails} problem(s) found.'}")
    return 1 if fails else 0


def _screen_test(cfg: Settings) -> None:
    """Captures one real frame through the configured backend (may show GNOME's picker)."""
    import time
    from .vision import Screen
    errs = []
    sc = Screen(cfg, errs.append)
    sc.start()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not sc.slot.get() and not errs:
        time.sleep(0.2)
    jpeg = sc.slot.get()
    sc.stop()
    if jpeg:
        out = os.path.join(os.path.expanduser("~"), "deskbot-screen-test.jpg")
        with open(out, "wb") as f:
            f.write(jpeg)
        _line(OK, f"Screen capture works ({len(jpeg) // 1024} KB frame saved to {out} for you to check)")
    else:
        _line(FAIL, f"Screen capture: {errs[0] if errs else 'no frame within 60 s'}",
              "Try Settings → Eyes → Capture method")
