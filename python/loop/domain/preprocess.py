"""Shared pre-processing, applied before any rung sees a message.

"HTML → text (strip style, script, tracking pixels, and any img), remove quoted
history (> blocks, On … wrote:, Il … ha scritto:), collapse whitespace, cap at
6 000 characters." (Spec §08)

Done by hand rather than with a sanitiser library because the output is not
HTML — it is plain text for a regex and a model — and because a dependency that
renders untrusted HTML is a larger attack surface than forty lines of tag
stripping that never executes anything.
"""

import re
from dataclasses import dataclass
from html import unescape

from .messages import Language
from .thresholds import MAX_TEXT_CHARS, REVIEW_EXCERPT_MAX_CHARS

_EXECUTABLE = re.compile(
    r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>|<head\b[^>]*>.*?</head>|<!--.*?-->",
    re.IGNORECASE | re.DOTALL,
)
# A 1×1 GIF is a read receipt, and an alt text is never worth the request.
_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ANCHOR = re.compile(
    r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.IGNORECASE | re.DOTALL
)
_BLOCK_TAG = re.compile(
    r"</?(?:p|div|br|tr|li|h[1-6]|table|blockquote|section|article)[^>]*>", re.IGNORECASE
)
_ANY_TAG = re.compile(r"<[^>]+>")


def _unwrap_anchor(match: re.Match[str]) -> str:
    """Keep the href so a posting URL survives, drop the anchor markup.

    In parentheses, not angle brackets: the TypeScript wrote `label <href>` and
    then removed it with the generic tag strip on the next line, so the posting
    URL this exists to preserve was never preserved on an HTML message.
    """
    href, label = match.group(1), match.group(2)
    text = _ANY_TAG.sub("", label).strip()
    if text and not text.lower().startswith(("http://", "https://")):
        return f"{text} ({href})"
    return f" {href} "


def html_to_text(html: str) -> str:
    text = _EXECUTABLE.sub(" ", html)
    text = _IMAGE.sub(" ", text)
    text = _ANCHOR.sub(_unwrap_anchor, text)
    text = _BLOCK_TAG.sub("\n", text)
    text = _ANY_TAG.sub(" ", text)
    # `html.unescape` where the TypeScript hand-decoded six entities plus the
    # numeric form. A superset, and the entities it adds were previously left
    # in the text as literal `&rsquo;`.
    return unescape(text)


# A recruiter thread accumulates the entire conversation on every reply, and
# leaving it in means a rung reads a rejection from three months ago as today's
# news — and, at rung 3, pays for the tokens.
_QUOTE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*On .{5,120}\s+wrote:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Il .{5,120}\s+ha scritto:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*Messaggio originale\s*-{2,}\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*From:\s.+\n\s*Sent:\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Da:\s.+\n\s*Inviato:\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^_{10,}\s*$", re.MULTILINE),
)

# Signature blocks add noise and, occasionally, an injection attempt.
_SIGNATURE = re.compile(r"^\s*--\s*$", re.MULTILINE)

_QUOTED_LINE = re.compile(r"^\s*>")


def strip_quoted_history(text: str) -> str:
    cuts = [m.start() for m in (marker.search(text) for marker in _QUOTE_MARKERS) if m]
    head = text[: min(cuts)] if cuts else text
    lines = head.split("\n")
    # Whatever survives may still carry a > block at the bottom.
    end = len(lines)
    while end > 0 and _QUOTED_LINE.match(lines[end - 1]):
        end -= 1
    return "\n".join(lines[:end])


def strip_signature(text: str) -> str:
    match = _SIGNATURE.search(text)
    return text[: match.start()] if match else text


@dataclass(frozen=True, slots=True)
class Normalised:
    text: str
    # Links found before the text was capped — a posting URL often sits late.
    links: tuple[str, ...]
    truncated: bool


_URL = re.compile(r"""https?://[^\s<>"')]+""", re.IGNORECASE)
_LINE_BREAKS = re.compile(r"\r\n?")
_INLINE_SPACE = re.compile("[ \t\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalise_message(*, text: str | None = None, html: str | None = None) -> Normalised:
    raw = html_to_text(html) if html else (text or "")
    links = tuple(dict.fromkeys(_URL.findall(raw)))[:20]

    body = strip_signature(strip_quoted_history(raw))
    body = _LINE_BREAKS.sub("\n", body)
    body = _INLINE_SPACE.sub(" ", body)
    body = _BLANK_LINES.sub("\n\n", body).strip()

    truncated = len(body) > MAX_TEXT_CHARS
    return Normalised(body[:MAX_TEXT_CHARS] if truncated else body, links, truncated)


_WHITESPACE = re.compile(r"\s+")


def excerpt(text: str, limit: int = REVIEW_EXCERPT_MAX_CHARS) -> str:
    """≤280 chars, whole words, for a review card. The only text ever persisted."""
    flat = _WHITESPACE.sub(" ", text).strip()
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    last_space = cut.rfind(" ")
    return f"{cut[:last_space] if last_space > limit * 0.6 else cut}…"


_ITALIAN = re.compile(
    r"\b(?:il|la|per|non|che|della|candidatura|colloquio|grazie|cordiali|saluti)\b",
    re.IGNORECASE,
)
_ENGLISH = re.compile(
    r"\b(?:the|for|your|with|application|interview|thanks|regards|we|have)\b", re.IGNORECASE
)


def detect_language(text: str) -> Language:
    """Rough — enough to pick a follow-up template, and nothing more."""
    italian = len(_ITALIAN.findall(text))
    english = len(_ENGLISH.findall(text))
    if italian == 0 and english == 0:
        return "other"
    return "it" if italian > english else "en"
