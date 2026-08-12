"""The cross-vendor intent vocabulary.

Rung 3 exists because "unknown template, human-written email, Italian/English
mixed prose" cannot be pattern-matched. That is true of the *general* case — but
not of the commonest sentences in recruiting, which are close to formulaic in
both languages: "non proseguiremo", "vorremmo invitarti", "grazie per la tua
candidatura".

Reading those without a model matters on the machine this runs on: an 8 GB
laptop, where a resident 7B alongside Postgres and eight services is not a trade
worth making. So the phrases a rule can honestly recognise are recognised, and
everything genuinely ambiguous still goes to a human.
"""

import re
from dataclasses import dataclass

from loop.domain.messages import Intent


@dataclass(frozen=True, slots=True)
class Phrase:
    intent: Intent
    confidence: float
    pattern: re.Pattern[str]


def _phrase(intent: Intent, confidence: float, pattern: str) -> Phrase:
    return Phrase(intent, confidence, re.compile(pattern, re.IGNORECASE))


PHRASES: tuple[Phrase, ...] = (
    # Rejections. The most common message in any job search, and the one whose
    # absence leaves an application sitting "live" forever.
    _phrase(
        "rejected",
        0.95,
        r"\b(?:not\s+(?:be\s+)?mov(?:e|ing)\s+forward|decision\s+to\s+not\s+move\s+forward"
        r"|moving\s+forward\s+with\s+other|other\s+candidates|decided\s+not\s+to\s+proceed"
        r"|will\s+not\s+be\s+progressing|unable\s+to\s+offer\s+you"
        r"|not\s+(?:been\s+)?select(?:ed)?)\b",
    ),
    _phrase(
        "rejected",
        0.95,
        r"\b(?:non\s+(?:siamo|sarà|possiamo)\s+(?:in\s+grado\s+)?(?:di\s+)?"
        r"(?:proceder|prosegui|dar\s+seguito)|abbiamo\s+deciso\s+di\s+non\s+proceder"
        r"|non\s+proseguir(?:e|emo)|non\s+è\s+stata\s+selezionata"
        r"|ti\s+terremo\s+in\s+considerazione\s+per\s+future|non\s+abbiamo\s+individuato"
        r"|purtroppo\s+(?:non|la\s+tua))\b",
    ),
    # Acknowledgements.
    _phrase(
        "acknowledged",
        0.94,
        r"\b(?:thank\s+you\s+for\s+(?:applying|your\s+application)"
        r"|we\s+(?:have\s+)?received\s+your\s+application"
        r"|appreciate\s+your\s+interest\s+in\s+joining)\b",
    ),
    _phrase(
        "acknowledged",
        0.94,
        r"\b(?:grazie\s+per\s+(?:la\s+tua\s+candidatura|averci\s+inviato|il\s+tuo\s+interesse)"
        r"|abbiamo\s+ricevuto\s+la\s+tua\s+candidatura)\b",
    ),
    # Invitations.
    _phrase(
        "interview_invite",
        0.9,
        r"\b(?:would\s+like\s+to\s+invite\s+you|invite\s+you\s+to\s+(?:an?\s+)?(?:interview|call)"
        r"|schedule\s+(?:a|an)\s+(?:call|interview|chat)"
        r"|next\s+step\s+in\s+(?:the|our)\s+process)\b",
    ),
    _phrase(
        "interview_invite",
        0.9,
        r"\b(?:vorremmo\s+invitarti|fissare\s+un\s+colloquio"
        r"|organizzare\s+un(?:\s+breve)?\s+(?:colloquio|call)"
        r"|disponibilità\s+per\s+un\s+colloquio)\b",
    ),
    _phrase(
        "take_home",
        0.9,
        r"\b(?:take[-\s]home|coding\s+(?:exercise|challenge)|technical\s+assignment"
        r"|prova\s+tecnica|esercizio\s+tecnico)\b",
    ),
)


def match_phrase(text: str) -> Phrase | None:
    """The first phrase that fits, or None. Order is precedence."""
    return next((p for p in PHRASES if p.pattern.search(text)), None)
