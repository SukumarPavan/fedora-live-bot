"""Settings: defaults, provider presets, load/save (JSON, owner-only permissions)."""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

APP_NAME = "DeskBot"
APP_ID = "deskbot"


def _base_dir(env: str, fallback: str, win_env: str) -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get(win_env, Path.home())) / APP_ID
    return Path(os.environ.get(env) or (Path.home() / fallback)) / APP_ID


CONFIG_DIR = _base_dir("XDG_CONFIG_HOME", ".config", "APPDATA")
STATE_DIR = _base_dir("XDG_STATE_HOME", ".local/state", "LOCALAPPDATA")
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LOG_FILE = STATE_DIR / "deskbot.log"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
VAD_MODEL = ASSETS_DIR / "silero_vad.onnx"

# OpenAI-compatible providers. Any other service works through "custom".
PROVIDERS: dict[str, dict[str, Any]] = {
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "key_url": "https://console.groq.com/keys",
        # text only (Groq has no vision model as of 2026-10): screen/cam images are dropped
        "chat_models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
        "stt_models": ["whisper-large-v3-turbo", "whisper-large-v3"],
        "tts_models": [],
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_url": "https://aistudio.google.com/apikey",
        # measured 2026-10-30 on a free key: lite models answer in ~1 s, big Flash models
        # were overloaded (503) or hung > 30 s
        "chat_models": ["gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                        "gemini-3.5-flash", "gemini-flash-latest"],
        "stt_models": [],
        "tts_models": [],
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_url": "https://openrouter.ai/keys",
        "chat_models": ["openrouter/auto", "google/gemma-3-27b-it:free"],
        "stt_models": [],
        "tts_models": [],
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "key_url": "https://platform.openai.com/api-keys",
        "chat_models": ["gpt-4o-mini", "gpt-4.1-mini"],
        "stt_models": ["whisper-1", "gpt-4o-mini-transcribe"],
        "tts_models": ["gpt-4o-mini-tts", "tts-1"],
    },
    "mistral": {
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "key_url": "https://console.mistral.ai/api-keys",
        "chat_models": ["pixtral-12b-2409", "mistral-small-latest"],
        "stt_models": [],
        "tts_models": [],
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "key_url": "https://platform.deepseek.com/api_keys",
        "chat_models": ["deepseek-chat"],
        "stt_models": [],
        "tts_models": [],
    },
    "custom": {
        "label": "Custom (any OpenAI-compatible URL)",
        "base_url": "",
        "key_url": "",
        "chat_models": [],
        "stt_models": [],
        "tts_models": [],
    },
}

LIVE_VOICES = ["Charon", "Fenrir", "Orus", "Puck", "Kore", "Aoede", "Leda", "Zephyr"]

EMOTIONS = ["neutral", "happy", "excited", "love", "wink", "sad", "angry",
            "surprised", "scared", "thinking", "sleepy", "error"]

DEFAULT_PERSONALITY = (
    "You are {name}, a tiny, cute but slightly sassy robot who floats on the user's "
    "desktop. You are witty, warm and helpful, like a pocket-sized AI sidekick. "
    "You talk like a real friend on a voice call: short, natural sentences, no lists, "
    "no markdown, no emojis. Usually reply in 1-3 sentences unless asked for more."
)

LANGUAGE_NOTES = {
    "english": "Reply in English.",
    "english_telugu": (
        "The user speaks English, Telugu, or a mix (Tenglish). Match the language of their "
        "LAST message: if it was English only, reply in English only; if it had Telugu, you may "
        "mix. Always write Telugu words in Telugu script (తెలుగు), never in English letters, "
        "or the voice will mispronounce them."),
    "english_hindi": (
        "The user speaks English, Hindi, or a mix (Hinglish). Match the language of their "
        "LAST message: if it was English only, reply in English only. Always write Hindi words "
        "in Devanagari script, never in English letters."),
    "auto": "Reply in whatever language the user speaks, using its native script.",
}

CONFIG_VERSION = 2

DEFAULTS: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "bot": {
        "name": "Ultron Jr",
        "personality": DEFAULT_PERSONALITY,
        "about_me": "",
        "language": "english_telugu",
        "subtitles": True,
        "status_pill": True,
        "subtitle_seconds": 7,
    },
    "look": {
        "size": 1.0,
        "opacity": 1.0,
        "eye_color": "#ff2a2a",
        "body_tint": "#c3c8d0",
        "glow": 0.8,
        "fps": 45,
    },
    "move": {
        "mode": "wander",          # stay | wander | edges
        "style": "mix",            # hop | glide | mix
        "roam_every_s": 25,
        "speed": 1.0,
        "hop_height": 1.0,
        "bob": 1.0,
        "still_while_talking": True,
        "screen": "primary",       # primary | all
    },
    "window": {
        "overlay_mode": "overlay",  # overlay (bypass window manager) | normal
        "force_xwayland": True,
        "x": None,
        "y": None,
    },
    "engine": {"mode": "universal"},  # universal | live
    "keys": {name: "" for name in PROVIDERS},
    "custom_base_url": "",
    "brain": {
        "provider": "gemini",
        "model": "gemini-flash-lite-latest",
        "fallback_models": "gemini-3.5-flash-lite, gemini-3.1-flash-lite",
        "temperature": 0.8,
        "max_tokens": 600,
        "history_turns": 12,
        "reasoning_effort": "default",  # default | none | low | medium | high
    },
    "ears": {
        "provider": "groq",
        "model": "whisper-large-v3-turbo",
        "language": "",            # empty = auto detect
    },
    "voice": {
        "engine": "edge",          # edge | openai | espeak
        "edge_voice": "en-US-ChristopherNeural",
        "edge_voice_alt": "te-IN-MohanNeural",
        "alt_auto": True,
        "rate": 5,                 # percent
        "pitch": -8,               # Hz
        "volume": 100,             # percent (output gain)
        "robot_effect": 0.25,      # 0..1
        "openai_provider": "openai",
        "openai_model": "gpt-4o-mini-tts",
        "openai_voice": "onyx",
    },
    "live": {
        "model": "auto",
        "voice": "Charon",
        "language_code": "",
        "video_fps": 1.0,
        "start_sensitivity": "low",   # low | high
        "end_sensitivity": "low",
        "silence_ms": 600,
        "affective": False,
        "robot_effect": True,
    },
    "listen": {
        "mic_on": True,
        "mode": "always",          # always | name | push
        "input_device": "",
        "output_device": "",
        "vad_threshold": 0.5,
        "silence_ms": 600,
        "min_speech_ms": 250,
        "barge_in": True,
        "barge_threshold": 0.85,
        "barge_ms": 300,
        "echo_margin": 2.5,        # while the bot talks you must be this much louder than its echo
        "half_duplex": False,
    },
    "eyes": {
        "screen_on": False,
        "cam_on": False,
        "screen_backend": "auto",  # auto | portal | mss | command
        "screen_command": "spectacle -b -n -f -o {out}",
        "screen_monitor": 1,
        "screen_max_width": 1280,
        "screen_fps": 1.0,
        "restore_token": "",
        "gst_template": (
            "pipewiresrc fd={fd} path={node} ! videorate drop-only=true max-rate={fps} ! "
            "videoconvert ! videoscale ! video/x-raw,width={width},pixel-aspect-ratio=1/1 ! "
            "jpegenc quality={quality} ! fdsink fd=1"),
        "cam_index": 0,
        "cam_max_width": 640,
        "jpeg_quality": 70,
    },
    "proactive": {
        "level": "sometimes",      # off | sometimes | chatty
        "min_interval_s": 120,
        "change_threshold": 0.12,
    },
    "system": {
        "autostart": False,
        "log_level": "INFO",
        "sleep_after_min": 10,
    },
}

PROACTIVE_PRESETS = {"off": None, "sometimes": (120, 0.12), "chatty": (30, 0.05)}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    """Dotted-key settings store, e.g. settings.get("brain.model")."""

    def __init__(self, path: Path = SETTINGS_FILE):
        self.path = path
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._listeners: list[Callable[[str, Any], None]] = []
        self._lock = threading.RLock()
        self._save_timer: threading.Timer | None = None

    def load(self) -> "Settings":
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text("utf-8"))
                self.data = _merge(DEFAULTS, raw)
                self._migrate(int(raw.get("config_version", 1)))
        except Exception as e:  # corrupt file: keep defaults, keep a backup
            log.error("Could not read settings (%s); using defaults", e)
            try:
                self.path.replace(self.path.with_suffix(".broken.json"))
            except OSError:
                pass
        return self

    def _migrate(self, version: int) -> None:
        if version < 2:
            # v1 defaults hung or failed on free Gemini keys (see PROVIDERS["gemini"])
            b = self.data["brain"]
            if b.get("provider") == "gemini" and b.get("model") in ("gemini-flash-latest", "gemini-2.5-flash"):
                b["model"] = DEFAULTS["brain"]["model"]
            if b.get("reasoning_effort") == "none":
                b["reasoning_effort"] = "default"
            if b.get("max_tokens") == 350:
                b["max_tokens"] = 600
            log.info("Settings migrated to version 2")
        self.data["config_version"] = CONFIG_VERSION

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def save_soon(self, delay: float = 0.8) -> None:
        with self._lock:
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(delay, self.save)
            self._save_timer.daemon = True
            self._save_timer.start()

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value: Any, save: bool = True) -> None:
        with self._lock:
            parts = key.split(".")
            node = self.data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            if node.get(parts[-1]) == value:
                return
            node[parts[-1]] = value
        if save:
            self.save_soon()
        for cb in list(self._listeners):
            try:
                cb(key, value)
            except Exception:
                log.exception("settings listener failed for %s", key)

    def on_change(self, cb: Callable[[str, Any], None]) -> None:
        self._listeners.append(cb)

    def reset(self) -> None:
        keys = copy.deepcopy(self.data.get("keys", {}))
        self.data = copy.deepcopy(DEFAULTS)
        self.data["keys"] = keys
        self.save()
        for cb in list(self._listeners):
            cb("*", None)

    # --- provider helpers -------------------------------------------------
    def base_url(self, provider: str) -> str:
        if provider == "custom":
            return self.get("custom_base_url", "")
        return PROVIDERS.get(provider, {}).get("base_url", "")

    def key(self, provider: str) -> str:
        return (self.get(f"keys.{provider}") or "").strip()

    def system_prompt(self, extra: str = "") -> str:
        name = self.get("bot.name") or "Bot"
        parts = [self.get("bot.personality", DEFAULT_PERSONALITY).replace("{name}", name),
                 LANGUAGE_NOTES.get(self.get("bot.language"), LANGUAGE_NOTES["auto"])]
        about = (self.get("bot.about_me") or "").strip()
        if about:
            parts.append(f"About the user: {about}")
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)
