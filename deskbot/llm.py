"""OpenAI-compatible API helpers: speech-to-text, streaming chat with images, model lists.

Works with any provider that speaks the OpenAI REST dialect (Groq, Gemini's OpenAI
endpoint, OpenRouter, OpenAI, Mistral, DeepSeek, Together, Cerebras, xAI, self-hosted...)."""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import AsyncIterator

import numpy as np
import openai

from .audio_io import wav_bytes
from .config import PROVIDERS, Settings

log = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """An error with a message that is safe and friendly to show to the user."""


_clients: dict[tuple, openai.AsyncOpenAI] = {}


class _Shared:
    """Wraps a cached client so callers' `await client.close()` doesn't close it."""

    def __init__(self, client: openai.AsyncOpenAI):
        self._c = client

    def __getattr__(self, name):
        return getattr(self._c, name)

    async def close(self) -> None:
        pass


def make_client(cfg: Settings, provider: str, timeout: float = 60.0,
                max_retries: int = 1) -> openai.AsyncOpenAI:
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    k = (cfg.base_url(provider), cfg.key(provider), timeout, max_retries, loop_id)
    if loop_id and k[0] and k[1]:
        if k not in _clients:
            if len(_clients) > 32:
                _clients.clear()
            _clients[k] = _new_client(cfg, provider, timeout, max_retries)
        return _Shared(_clients[k])
    return _new_client(cfg, provider, timeout, max_retries)


def _new_client(cfg: Settings, provider: str, timeout: float, max_retries: int) -> openai.AsyncOpenAI:
    base = cfg.base_url(provider)
    key = cfg.key(provider)
    label = PROVIDERS.get(provider, {}).get("label", provider)
    if not base:
        raise ApiError(f"No base URL set for {label}. Open Settings → Brain.")
    if not key:
        raise ApiError(f"No API key for {label}. Open Settings → Brain and paste your key.")
    return openai.AsyncOpenAI(base_url=base, api_key=key, timeout=timeout, max_retries=max_retries)


KEY_HELP = {
    "gemini": "Make a NEW key at aistudio.google.com/apikey → Create API key, and paste it on Settings → API Keys.",
    "groq": "Make a new key at console.groq.com/keys and paste it on Settings → API Keys.",
}


def key_problem(text: str, provider: str) -> str | None:
    """A clear message if an error text means the API key itself is bad, else None."""
    t = text.lower()
    label = PROVIDERS.get(provider, {}).get("label", provider).removeprefix("Google ")
    fix = KEY_HELP.get(provider, "Check it on Settings → API Keys.")
    if "account_state_invalid" in t or "service account" in t:
        return f"Google has disabled this {label} key (its account was deleted or turned off). {fix}"
    if any(w in t for w in ("api_key_invalid", "api key not valid", "invalid api key", "invalid_api_key",
                            "incorrect api key", "unauthenticated", "unauthorized", "401", "permission_denied")):
        return f"My {label} key was rejected. {fix}"
    return None


def friendly_error(e: Exception, provider: str) -> str:
    label = PROVIDERS.get(provider, {}).get("label", provider)
    if isinstance(e, ApiError):
        return str(e)
    if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return key_problem(_short(e), provider) or f"My {label} key was rejected. {KEY_HELP.get(provider, '')}"
    if isinstance(e, openai.RateLimitError):
        return f"{label} free limit reached. Give me a minute."
    if isinstance(e, openai.NotFoundError):
        return f"{label} doesn't know that model. Pick another in Settings (Fetch models)."
    if isinstance(e, openai.APIConnectionError):
        return f"Can't reach {label}. Is the internet on?"
    if isinstance(e, (openai.APITimeoutError, asyncio.TimeoutError)):
        return f"{label} took too long to answer."
    if isinstance(e, openai.APIStatusError):
        text = _short(e).lower()
        bad_key = key_problem(text, provider)
        if bad_key and e.status_code in (400, 401, 403):
            return bad_key
        if "quota" in text or "rate limit" in text or "resource_exhausted" in text:
            return f"{label} free limit reached. Give me a minute."
        if "model" in text and ("not found" in text or "does not exist" in text or "not supported" in text):
            return f"{label} doesn't know that model. Pick another in Settings (Fetch models)."
        return f"{label} error {e.status_code}: {_short(e)[:160]}"
    return f"{label}: {e}"


def _short(e: Exception) -> str:
    """Error text including the response body (some providers put the reason only there)."""
    parts = [str(getattr(e, "message", "") or e)]
    body = getattr(e, "body", None)
    if body:
        parts.append(str(body))
    resp = getattr(e, "response", None)
    try:
        if resp is not None and not body:
            parts.append(resp.text[:400])
    except Exception:
        pass
    return " ".join(parts)[:600]


def image_part(jpeg: bytes) -> dict:
    return {"type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}


async def transcribe(cfg: Settings, pcm16: np.ndarray) -> str:
    provider = cfg.get("ears.provider")
    client = make_client(cfg, provider)
    kwargs = {}
    lang = (cfg.get("ears.language") or "").strip()
    if lang:
        kwargs["language"] = lang
    name = cfg.get("bot.name") or ""
    if name:
        kwargs["prompt"] = f"Conversation with a robot named {name}."
    try:
        res = await client.audio.transcriptions.create(
            file=("speech.wav", wav_bytes(pcm16), "audio/wav"),
            model=cfg.get("ears.model"), **kwargs)
    except Exception as e:
        raise ApiError(friendly_error(e, provider)) from e
    finally:
        await client.close()
    return (getattr(res, "text", None) or str(res)).strip()


FIRST_TOKEN_TIMEOUT = 8.0    # a model that hasn't started answering by now is skipped
BACKUP_ERRORS = (openai.RateLimitError, openai.InternalServerError, openai.NotFoundError,
                 openai.APITimeoutError, openai.APIConnectionError, asyncio.TimeoutError)


class ChatOptions:
    """Remembers per-provider/model problems found at runtime."""
    no_reasoning_param: set[str] = set()
    no_vision: set[str] = set()
    cooldown: dict[str, float] = {}   # ident -> monotonic time until which it is skipped
    key_failed: dict[str, float] = {}  # provider -> until when its key is known bad


def _strip_images(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if isinstance(m.get("content"), list):
            text = " ".join(p.get("text", "") for p in m["content"] if p.get("type") == "text")
            out.append({**m, "content": text})
        else:
            out.append(m)
    return out


def model_chain(cfg: Settings) -> list[str]:
    """Main model first, then backups; models that just failed go to the back."""
    provider = cfg.get("brain.provider")
    main = cfg.get("brain.model")
    extra = [m.strip() for m in str(cfg.get("brain.fallback_models") or "").split(",") if m.strip()]
    chain = [main] + [m for m in extra if m != main]
    now = time.monotonic()
    ok = [m for m in chain if ChatOptions.cooldown.get(f"{provider}:{m}", 0) <= now]
    return ok + [m for m in chain if m not in ok]


async def _open_stream(client, cfg: Settings, model: str, messages: list[dict], provider: str):
    ident = f"{provider}:{model}"
    kwargs = {"model": model, "temperature": float(cfg.get("brain.temperature", 0.8)),
              "max_tokens": int(cfg.get("brain.max_tokens", 600)), "stream": True}
    effort = cfg.get("brain.reasoning_effort", "default")
    if effort and effort != "default" and ident not in ChatOptions.no_reasoning_param:
        kwargs["reasoning_effort"] = effort
    if ident in ChatOptions.no_vision:
        messages = _strip_images(messages)
    for _ in range(3):
        try:
            return await asyncio.wait_for(client.chat.completions.create(messages=messages, **kwargs),
                                          FIRST_TOKEN_TIMEOUT)
        except openai.BadRequestError as e:
            text = _short(e).lower()
            if "reasoning_effort" in kwargs:  # optional; many models reject it with a vague 400
                log.warning("%s rejected reasoning_effort; retrying without it", ident)
                ChatOptions.no_reasoning_param.add(ident)
                kwargs.pop("reasoning_effort")
                continue
            has_images = any(isinstance(m.get("content"), list) for m in messages)
            if has_images and any(w in text for w in ("image", "vision", "multimodal", "content")):
                log.warning("%s seems to have no vision; retrying without images", ident)
                ChatOptions.no_vision.add(ident)
                messages = _strip_images(messages)
                continue
            raise
    raise ApiError("The AI rejected the request.")


BACKUP_BRAIN = ("groq", "openai/gpt-oss-20b")  # text-only, ~1 s on Groq's free tier
_backup_noted = {"done": False}


async def chat_stream(cfg: Settings, messages: list[dict]) -> AsyncIterator[str]:
    """Streams the reply. If the chosen provider fails completely (bad key, outage) and a Groq
    key exists, answers through Groq instead of leaving the user in silence."""
    provider = cfg.get("brain.provider")
    try:
        async for t in _chat_on(cfg, provider, model_chain(cfg), messages):
            yield t
        return
    except ApiError as e:
        bp, bm = BACKUP_BRAIN
        if provider == bp or not cfg.key(bp) or not cfg.get("brain.cross_backup", True):
            raise
        log.warning("Brain %s failed (%s); answering through backup %s/%s", provider, e, bp, bm)
        primary_err = "" if _backup_noted["done"] else str(e)
        _backup_noted["done"] = True
    async for t in _chat_on(cfg, bp, [bm], _strip_images(messages)):
        yield t
    if primary_err:
        raise BackupUsed(primary_err)


class BackupUsed(ApiError):
    """Raised AFTER a complete backup answer: the caller may tell the user once why."""


async def _chat_on(cfg: Settings, provider: str, chain: list[str], messages: list[dict]) -> AsyncIterator[str]:
    client = make_client(cfg, provider, timeout=30, max_retries=0)
    last: Exception | None = None
    try:
        for model in chain:
            ident = f"{provider}:{model}"
            started = False
            try:
                t0 = time.monotonic()
                it = (await _open_stream(client, cfg, model, messages, provider)).__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(it.__anext__(), 30 if started else FIRST_TOKEN_TIMEOUT)
                    except StopAsyncIteration:
                        break
                    text = chunk.choices[0].delta.content if chunk.choices else None
                    if text:
                        if not started:
                            log.info("Brain %s answered in %.1fs", model, time.monotonic() - t0)
                        started = True
                        yield text
                if started:
                    ChatOptions.cooldown.pop(ident, None)
                    return
                last = ApiError(f"{model} gave an empty answer")
                log.warning("Brain %s gave an empty answer; trying a backup", model)
            except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
                ChatOptions.key_failed[provider] = time.monotonic() + 300
                raise ApiError(friendly_error(e, provider)) from e  # same key for every model
            except BACKUP_ERRORS as e:
                if started:
                    raise ApiError(friendly_error(e, provider)) from e
                log.warning("Brain %s failed (%s); trying a backup", model, type(e).__name__)
                ChatOptions.cooldown[ident] = time.monotonic() + 180
                last = e
        if isinstance(last, ApiError):
            raise last
        if isinstance(last, asyncio.TimeoutError):
            raise ApiError("My brain is too busy right now (all models timed out). Try again in a moment.")
        raise ApiError(friendly_error(last, provider) if last else "No brain model configured.")
    except ApiError:
        raise
    except Exception as e:
        raise ApiError(friendly_error(e, provider)) from e
    finally:
        await client.close()


async def complete(cfg: Settings, messages: list[dict]) -> str:
    out = []
    try:
        async for t in chat_stream(cfg, messages):
            out.append(t)
    except BackupUsed:
        pass
    return "".join(out)


async def list_models(cfg: Settings, provider: str) -> list[str]:
    client = make_client(cfg, provider, timeout=20)
    try:
        page = await client.models.list()
        ids = sorted({m.id.removeprefix("models/") for m in page.data})
        return ids
    except Exception as e:
        raise ApiError(friendly_error(e, provider)) from e
    finally:
        await client.close()


def vision_note(cfg: Settings) -> str | None:
    ident = f"{cfg.get('brain.provider')}:{cfg.get('brain.model')}"
    if ident in ChatOptions.no_vision:
        return "My current brain model can't see images. Pick a vision model in Settings."
    return None
