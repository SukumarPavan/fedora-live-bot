"""Linux microphone through PipeWire/PulseAudio (`parecord`), with automatic mic choice.

PortAudio only sees ALSA's "default" device, so a bad system default (a muted mic, a monitor,
or a broken echo-cancel source) silently gives the bot pure silence. Here every PipeWire
source can be targeted by name, and "Auto" probes each one and picks one that hears sound."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .audio_io import MIC_BLOCK, MIC_SR

log = logging.getLogger(__name__)

EC_SOURCE = "deskbot_ec_source"
DEAD_PEAK = 1e-4      # ~3 LSB of 16-bit audio: anything quieter is digital silence, not a room
REC_ARGS = ["--raw", "--format=s16le", f"--rate={MIC_SR}", "--channels=1", "--latency-msec=40",
            "--client-name=DeskBot"]


@dataclass
class Source:
    name: str
    description: str
    monitor: bool = False
    muted: bool = False


def available() -> bool:
    if not sys.platform.startswith("linux") or not (shutil.which("parecord") and shutil.which("pactl")):
        return False
    try:
        return subprocess.run(["pactl", "info"], capture_output=True, timeout=3).returncode == 0
    except Exception:
        return False


def list_sources() -> list[Source]:
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True,
                             text=True, timeout=5).stdout
        result = []
        for s in json.loads(out):
            name = s.get("name", "")
            props = s.get("properties") or {}
            monitor = bool(s.get("monitor_source")) or name.endswith(".monitor") \
                or props.get("device.class") == "monitor"
            result.append(Source(name, s.get("description") or name, monitor, bool(s.get("mute"))))
        return result
    except Exception:
        pass
    try:  # older pactl without JSON output
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True,
                             text=True, timeout=5).stdout
        return [Source(n, n, n.endswith(".monitor")) for n in
                (line.split("\t")[1] for line in out.splitlines() if "\t" in line)]
    except Exception:
        return []


def default_source() -> str:
    try:
        return subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except Exception:
        return ""


def describe(name: str) -> str:
    for s in list_sources():
        if s.name == name:
            return s.description
    return name


def set_default_source(name: str) -> bool:
    try:
        return subprocess.run(["pactl", "set-default-source", name], capture_output=True,
                              timeout=3).returncode == 0
    except Exception:
        return False


def probe_peak(name: str, seconds: float = 1.0) -> float:
    """Loudest sample (0..1) recorded from a source in `seconds`; -1 if it can't record."""
    try:
        proc = subprocess.Popen(["parecord", *REC_ARGS, f"--device={name}"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        want = int(MIC_SR * seconds) * 2
        data = b""
        timer = threading.Timer(seconds + 2.0, proc.kill)
        timer.start()
        while len(data) < want:
            chunk = proc.stdout.read(want - len(data))
            if not chunk:
                break
            data += chunk
        timer.cancel()
        proc.kill()
        proc.wait(timeout=2)
        if len(data) < want // 4:
            return -1.0
        x = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16)
        # skip the first 100 ms: some sources start with a click or with silence
        x = x[MIC_SR // 10:] if len(x) > MIC_SR // 5 else x
        return float(np.max(np.abs(x.astype(np.int32)))) / 32768.0
    except Exception as e:
        log.warning("Probe of %s failed: %s", name, e)
        return -1.0


def choose_source(preferred: str = "") -> tuple[str, list[str], str]:
    """Pick the mic to use. Returns (source_name, report_lines, user_notice)."""
    sources = {s.name: s for s in list_sources() if not s.monitor}
    if preferred:
        if preferred in sources:
            return preferred, [f"Using the mic chosen in Settings: {sources[preferred].description}"], ""
        notice = "The mic chosen in Settings is gone; picking one automatically."
    else:
        notice = ""
    default = default_source()
    order = []
    if EC_SOURCE in sources:
        order.append(EC_SOURCE)
    if default in sources and default not in order:
        order.append(default)
    order += [n for n in sources if n not in order]
    report = []
    chosen = ""
    for name in order:
        src = sources[name]
        if src.muted:
            report.append(f"{src.description}: muted, skipped")
            continue
        peak = probe_peak(name)
        alive = peak >= DEAD_PEAK
        report.append(f"{src.description} [{name}]: peak {peak:.5f} → {'hears sound' if alive else 'SILENT'}")
        if alive:
            chosen = name
            break
    if not chosen:
        chosen = default if default in sources else (order[0] if order else "")
        notice = notice or ("Every microphone I tried is silent or muted. Check the mute switch / "
                            "volume in GNOME Settings → Sound → Input.")
        return chosen, report, notice
    # The old installer made the echo-cancel source the system default; if that is dead,
    # every app gets silence. Put the system default back on a mic that works.
    if default == EC_SOURCE and chosen != EC_SOURCE:
        if set_default_source(chosen):
            report.append(f"System default mic was the silent echo-cancel source; reset to {chosen}")
            notice = (f"Echo cancellation wasn't passing any sound, so I switched your system mic back "
                      f"to {sources[chosen].description}.")
    return chosen, report, notice


class PulseMic:
    """Same interface as audio_io.Mic: on_block gets int16 blocks of MIC_BLOCK samples."""

    def __init__(self, on_block: Callable[[np.ndarray], None], on_dead: Callable[[str], None] | None = None):
        self.on_block = on_block
        self.on_dead = on_dead
        self.proc: subprocess.Popen | None = None
        self.device = ""
        self._stop = threading.Event()

    def start(self, device: str) -> None:
        self.stop()
        self._stop.clear()
        args = ["parecord", *REC_ARGS] + ([f"--device={device}"] if device else [])
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.device = device or "@DEFAULT_SOURCE@"
        threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="pulse-mic").start()
        log.info("Mic started via PipeWire: %s", self.device)

    def _read(self, proc: subprocess.Popen) -> None:
        nbytes = MIC_BLOCK * 2
        while not self._stop.is_set():
            data = proc.stdout.read(nbytes)
            if len(data) < nbytes:
                break
            try:
                self.on_block(np.frombuffer(data, dtype=np.int16).copy())
            except Exception:
                log.exception("mic consumer failed")
        if not self._stop.is_set():
            err = proc.stderr.read().decode(errors="replace")[-300:] if proc.stderr else ""
            log.error("Mic recorder stopped: %s", err.strip())
            if self.on_dead:
                self.on_dead(err.strip() or "recorder exited")

    def stop(self) -> None:
        self._stop.set()
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None
