"""Pure text helpers: sentence splitting, emotion tags, TTS cleanup, voice picking."""
from __future__ import annotations

import re
import unicodedata

TAG_RE = re.compile(r"\[(\w+)\]")
_SENTENCE_END = re.compile(r"(.+?[.!?।॥…]+[\"')\]]*)(\s+|$)", re.S)

# Phrases Whisper invents on silence or noise.
HALLUCINATIONS = {
    "thank you", "thank you.", "thanks for watching", "thanks for watching!",
    "thank you for watching", "you", "bye", "bye.", "subtitles by the amara.org community",
    "please subscribe", "like and subscribe", ".", "..", "...", "so", "okay", "ok",
    "thank you so much for watching", "i'll see you next time",
}


def split_sentences(buffer: str, min_chars: int = 12, force_chars: int = 160) -> tuple[list[str], str]:
    """Split finished sentences off a streaming buffer. Returns (sentences, remainder).

    Short fragments are merged forward so TTS calls are not wastefully tiny; very long
    runs without punctuation are cut at a comma or space so speech starts quickly."""
    out: list[str] = []
    pending = ""
    pos = 0
    for m in _SENTENCE_END.finditer(buffer):
        if m.end() == len(buffer) and not m.group(2):
            break  # punctuation at the very end might be "3." of "3.5": wait for more
        piece = (pending + m.group(1)).strip()
        pos = m.end()
        if len(piece) < min_chars:
            pending = piece + " "
            continue
        out.append(piece)
        pending = ""
    rest = pending + buffer[pos:]
    if len(rest) > force_chars:
        cut = max(rest.rfind(", ", 0, force_chars), rest.rfind(" ", 0, force_chars))
        if cut > 20:
            out.append(rest[:cut + 1].strip())
            rest = rest[cut + 1:].lstrip()
    if "\n" in rest:
        head, _, tail = rest.rpartition("\n")
        if head.strip():
            out.extend(s.strip() for s in head.split("\n") if s.strip())
        rest = tail
    return out, rest


def pop_leading_tag(text: str) -> tuple[str | None, str]:
    """'[happy] Hi!' -> ('happy', 'Hi!'). Returns any tag lower-cased; callers check it."""
    m = re.match(r"\s*\[(\w+)\]\s*", text)
    if not m:
        return None, text
    return m.group(1).lower(), text[m.end():]


def tag_pending(text: str) -> bool:
    """True while a stream may still be in the middle of its leading [tag]."""
    t = text.lstrip()
    return t.startswith("[") and "]" not in t and len(t) < 24


def strip_tags(text: str) -> str:
    return TAG_RE.sub("", text).strip()


_MD = re.compile(r"(\*\*|__|`+|^#+\s*|^\s*[-*•]\s+)", re.M)
_URL = re.compile(r"https?://\S+")


def clean_for_tts(text: str) -> str:
    text = strip_tags(text)
    text = _URL.sub("link", text)
    text = _MD.sub("", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("So", "Cs", "Co"))
    return re.sub(r"\s+", " ", text).strip()


def is_hallucination(text: str) -> bool:
    t = text.strip().lower()
    if len(re.sub(r"\W", "", t)) < 2:
        return True
    return t in HALLUCINATIONS or t.rstrip(".!") in HALLUCINATIONS


def non_latin_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ord(ch) > 0x024F) / len(letters)


def pick_voice(sentence: str, primary: str, alt: str, auto: bool) -> str:
    if auto and alt and non_latin_ratio(sentence) > 0.3:
        return alt
    return primary


def mentions_name(text: str, name: str) -> bool:
    words = [w for w in re.split(r"\W+", name.lower()) if len(w) > 2]
    t = text.lower()
    return any(w in t for w in words) if words else True


_EMO_WORDS = {
    "happy": ("haha", "great", "awesome", "nice", "glad", "yay", "cool", "love it"),
    "excited": ("wow", "amazing", "let's go", "incredible", "!!"),
    "sad": ("sorry", "sad", "unfortunately", "miss you", "oh no"),
    "angry": ("annoying", "hate", "stupid", "ugh"),
    "surprised": ("really?", "what?", "no way", "whoa"),
    "thinking": ("hmm", "let me think", "maybe", "perhaps"),
    "love": ("love you", "sweet", "aww", "cute"),
}


def guess_emotion(text: str) -> str | None:
    t = text.lower()
    best, score = None, 0
    for emo, words in _EMO_WORDS.items():
        s = sum(t.count(w) for w in words)
        if s > score:
            best, score = emo, s
    return best
