"""Needs a PulseAudio/PipeWire server: a live source 'real_mic' and a silent 'deskbot_ec_source'.
Skipped when none is reachable (CI); run with PULSE_SERVER pointing at a test server."""
import time

import numpy as np
import pytest

from deskbot import pulse_mic

pytestmark = pytest.mark.skipif(not pulse_mic.available(), reason="no PulseAudio/PipeWire server")


def names():
    return {s.name for s in pulse_mic.list_sources()}


@pytest.fixture
def broken_default():
    if not {"real_mic", "deskbot_ec_source"} <= names():
        pytest.skip("test server lacks real_mic/deskbot_ec_source")
    pulse_mic.set_default_source("deskbot_ec_source")
    yield
    pulse_mic.set_default_source("deskbot_ec_source")


def test_lists_sources_and_flags_monitors():
    srcs = {s.name: s for s in pulse_mic.list_sources()}
    assert srcs["real_mic"].monitor is False
    assert any(s.monitor for s in srcs.values())


def test_probe_tells_silence_from_sound():
    assert pulse_mic.probe_peak("real_mic") > 0.1
    assert 0 <= pulse_mic.probe_peak("deskbot_ec_source") < pulse_mic.DEAD_PEAK


def test_auto_never_uses_echo_cancel_and_repairs_default(broken_default):
    name, report, notice = pulse_mic.choose_source("")
    assert name == "real_mic"
    assert pulse_mic.default_source() == "real_mic"
    assert not any("deskbot_ec" in line and "peak" in line for line in report)   # never even probed
    assert "set your system mic back" in notice


def test_auto_uses_the_system_default_mic_first():
    pulse_mic.set_default_source("real_mic")
    name, report, notice = pulse_mic.choose_source("")
    assert name == "real_mic" and len(report) == 1 and notice == ""


def test_explicit_choice_is_respected():
    name, _, notice = pulse_mic.choose_source("real_mic")
    assert name == "real_mic" and notice == ""


def test_stream_delivers_blocks():
    got = []
    mic = pulse_mic.PulseMic(got.append)
    mic.start("real_mic")
    time.sleep(1.0)
    mic.stop()
    assert len(got) > 10 and all(len(b) == 512 for b in got)
    assert max(int(np.abs(b.astype(np.int32)).max()) for b in got) > 3000
