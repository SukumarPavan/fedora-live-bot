"""OpenAI-compatible API helpers: speech-to-text, streaming chat with images, model lists.

Works with any provider that speaks the OpenAI REST dialect (Groq, Gemini's OpenAI
endpoint, OpenRouter, OpenAI, Mistral, DeepSeek, Together, Cerebras, xAI, self-hosted...)."""
from __future__ import annotations

import base64
import logging
from typing import AsyncIterator

import numpy as np
import openai

from .audio_io import wav_bytes
from .config import PROVIDERS, Settings

log = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """An error with a message that is safe and friendly to show to the user."""


def make_client(cfg: Settings, provider: str, timeout: float = 60.0) -> openai.AsyncOpenAI:
    base = cfg.base_url(provider)
    key = cfg.key(provider)
    label = PROVIDERS.get(provider, {}).get("label", provider)
    if not base:
        raise ApiError(f"No base URL set for {label}. Open Settings → Brain.")
    if not key:
        raise ApiError(f"No API key for {label}. Open Settings → Brain and paste your key.")
    return openai.AsyncOpenAI(base_url=base, api_key=key, timeout=timeout, max_retries=1)


def friendly_error(e: Exception, provider: str) -> str:
    label = PROVIDERS.get(provider, {}).get("label", provider)
    if isinstance(e, ApiError):
        return str(e)
    if isinstance(e, openai.AuthenticationError):
        return f"My {label} key was rejected. Check it in Settings."
    if isinstance(e, openai.RateLimitError):
        return f"{label} free limit reached. Give me a minute."
    if isinstance(e, openai.NotFoundError):
        return f"{label} doesn't know that model. Pick another in Settings (Fetch models)."
    if isinstance(e, openai.APIConnectionError):
        return f"Can't reach {label}. Is the internet on?"
    if isinstance(e, openai.APITimeoutError):
        return f"{label} took too long to answer."
    if isinstance(e, openai.APIStatusError):
        text = _short(e).lower()
        if "api key" in text or "api_key" in text or "unauthorized" in text:
            return f"My {label} key was rejected. Check it in Settings."
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


class ChatOptions:
    """Remembers per-provider/model incompatibilities found at runtime."""
    no_reasoning_param: set[str] = set()
    no_vision: set[str] = set()


def _strip_images(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if isinstance(m.get("content"), list):
            text = " ".join(p.get("text", "") for p in m["content"] if p.get("type") == "text")
            out.append({**m, "content": text})
        else:
            out.append(m)
    return out


async def chat_stream(cfg: Settings, messages: list[dict]) -> AsyncIterator[str]:
    provider = cfg.get("brain.provider")
    model = cfg.get("brain.model")
    ident = f"{provider}:{model}"
    client = make_client(cfg, provider)
    kwargs = {"model": model, "temperature": float(cfg.get("brain.temperature", 0.8)),
              "max_tokens": int(cfg.get("brain.max_tokens", 350)), "stream": True}
    effort = cfg.get("brain.reasoning_effort", "default")
    if effort and effort != "default" and ident not in ChatOptions.no_reasoning_param:
        kwargs["reasoning_effort"] = effort
    if ident in ChatOptions.no_vision:
        messages = _strip_images(messages)
    try:
        for attempt in range(3):
            try:
                stream = await client.chat.completions.create(messages=messages, **kwargs)
                break
            except openai.BadRequestError as e:
                text = _short(e).lower()
                if "reasoning" in text and "reasoning_effort" in kwargs:
                    log.warning("%s rejects reasoning_effort; retrying without it", ident)
                    ChatOptions.no_reasoning_param.add(ident)
                    kwargs.pop("reasoning_effort")
                    continue
                if any(w in text for w in ("image", "vision", "multimodal", "content type")) \
                        and ident not in ChatOptions.no_vision:
                    log.warning("%s seems to have no vision; retrying without images", ident)
                    ChatOptions.no_vision.add(ident)
                    messages = _strip_images(messages)
                    continue
                raise
        else:
            raise ApiError("The AI rejected the request.")
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            text = getattr(delta, "content", None)
            if text:
                yield text
    except ApiError:
        raise
    except Exception as e:
        raise ApiError(friendly_error(e, provider)) from e
    finally:
        await client.close()


async def complete(cfg: Settings, messages: list[dict]) -> str:
    return "".join([t async for t in chat_stream(cfg, messages)])


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
