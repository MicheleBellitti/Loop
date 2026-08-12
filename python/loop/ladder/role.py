"""The role, from the body, when the subject did not carry it.

A job title is stated once — in the confirmation that the application was
received — and then never again. Every follow-up on that thread says "your
application" and assumes you remember which one. So a registry that only reads
subjects records the title for the handful of vendors that put it there and
leaves every other application as "Unknown role", which is not a gap in the
data: it is a gap in reading it.

Both languages, ordered most-specific first, and deliberately conservative. The
damage a greedy capture does here is a job called "here is a link to manage your
application data", so a candidate has to look like a job title before it is
believed.
"""

import re

# Six words is a generous ceiling for a job title and a low one for a sentence,
# which is exactly the discrimination needed here.
_MAX_WORDS = 6
_MIN_CHARS = 3
_MAX_CHARS = 60

_I = re.IGNORECASE
# Only the two label patterns are anchored per line; the rest anchor on the
# whole text, and widening them would let `$` match mid-body.
_IM = re.IGNORECASE | re.MULTILINE

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # English
    re.compile(
        r"\bapplication\s+for\s+the\s+(?:position\s+of\s+|role\s+of\s+)?"
        r"(?P<role>[^.,\n<>|]{3,60}?)\s*(?:position|role|opening|vacancy)?"
        r"\s*(?:[.,\n]|at\b|with\b|$)",
        _I,
    ),
    re.compile(
        r"\bapplied\s+(?:to|for)\s+(?:the\s+)?(?P<role>[^.,\n<>|]{3,60}?)"
        r"\s*(?:position|role|opening)\b",
        _I,
    ),
    re.compile(
        r"\bapply(?:ing)?\s+for\s+the\s+(?:position|role)\s+of\s+"
        r"(?P<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|at\b|with\b|$)",
        _I,
    ),
    re.compile(
        r"\b(?:position|role)\s+of\s+(?P<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|at\b|presso\b|$)",
        _I,
    ),
    re.compile(
        r"\byour\s+(?:candidacy|application)\s+(?:for|as)\s+(?:the\s+)?"
        r"(?P<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|at\b|$)",
        _I,
    ),
    re.compile(r"^\s*(?:position|role|job\s+title)\s*:\s*(?P<role>[^\n]{3,60})$", _IM),
    re.compile(
        r"\binterview\s+for\s+the\s+(?P<role>[^.,\n<>|]{3,60}?)\s*(?:position|role)\b", _I
    ),
    # Italian
    re.compile(
        r"\bcandidatura\s+(?:per|come)\s+(?:la\s+posizione\s+di\s+|il\s+ruolo\s+di\s+)?"
        r"(?P<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|presso\b|in\b|$)",
        _I,
    ),
    re.compile(r"\bposizione\s+di\s+(?P<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|presso\b|$)", _I),
    re.compile(r"^\s*(?:posizione|ruolo)\s*:\s*(?P<role>[^\n]{3,60})$", _IM),
    re.compile(
        r"\bcolloquio\s+(?:per|come)\s+(?:la\s+posizione\s+di\s+)?"
        r"(?P<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|presso\b|$)",
        _I,
    ),
    re.compile(
        r"\bopportunità\s+(?:di|come)\s+(?P<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|presso\b|$)", _I
    ),
)

# Words that make a phrase plausibly a job rather than a sentence.
_VOCABULARY = re.compile(
    r"engineer|developer|scientist|manager|analyst|designer|consultant|architect|specialist"
    r"|researcher|intern|lead|director|programmer|administrator|technician|ingegner|sviluppat"
    r"|analista|consulente|responsabile|stagista|tirocinan|progettista|tecnico|ricercator",
    re.IGNORECASE,
)

_NOT_A_TITLE = re.compile(r"[@]|https?:|\bunsubscribe\b|\bclick\b", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCTUATION = re.compile(r"[-–—:;,]+$")

# Where the title ends and the sentence resumes.
#
# The captures above are lazy but their terminators are optional, so a subject
# like "Your application for Platform Engineer was sent to Nexi" fits inside the
# six-word ceiling and arrives as a job title. Cutting at the first word no job
# title contains recovers the real one instead of discarding the match.
#
# Conjunctions and prepositions are deliberately absent: "Head of Data and
# Analytics" is one job.
_SENTENCE_RESUMES = re.compile(
    r"\b(?:was|were|is|are|has|have|been|sent|will|we|you|your|our"
    r"|è|sono|stata|stato|inviata|inviato|abbiamo|tua|tuo)\b.*$",
    re.IGNORECASE,
)


def role_from_body(text: str) -> str | None:
    for pattern in _PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        candidate = _clean(match.group("role") or "")
        if candidate is not None:
            return candidate
    return None


def _clean(raw: str) -> str | None:
    role = _WHITESPACE.sub(" ", raw).strip()
    role = _TRAILING_PUNCTUATION.sub("", _SENTENCE_RESUMES.sub("", role).strip()).strip()
    if not _MIN_CHARS <= len(role) <= _MAX_CHARS:
        return None
    if _NOT_A_TITLE.search(role):
        return None
    if len(role.split()) > _MAX_WORDS:
        return None
    if not _VOCABULARY.search(role):
        return None
    return role
