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
from typing import Literal

from loop.domain.messages import Intent

# Where a phrase came from. The differential harness reads this: a message the
# TypeScript could not place and this can is only a porting error if the phrase
# that placed it was supposed to exist in both.
Origin = Literal["typescript", "recall-audit"]


@dataclass(frozen=True, slots=True)
class Phrase:
    intent: Intent
    confidence: float
    pattern: re.Pattern[str]
    origin: Origin = "typescript"


def _phrase(
    intent: Intent, confidence: float, pattern: str, origin: Origin = "typescript"
) -> Phrase:
    return Phrase(intent, confidence, re.compile(pattern, re.IGNORECASE), origin)


PHRASES: tuple[Phrase, ...] = (
    # Rejections. The most common message in any job search, and the one whose
    # absence leaves an application sitting "live" forever.
    _phrase(
        "rejected",
        0.95,
        r"\b(?:not\s+(?:be\s+)?mov(?:e|ing)\s+forward|decision\s+to\s+not\s+move\s+forward"
        r"|moving\s+forward\s+with\s+other|other\s+candidates|decided\s+not\s+to\s+proceed"
        r"|will\s+not\s+be\s+progressing|unable\s+to\s+offer\s+you"
        r"|no\s+longer\s+be\s+interviewing|we\s+have\s+filled\s+the\b"
        r"|not\s+(?:been\s+)?select(?:ed)?)\b",
        # `no longer be interviewing` and `we have filled the` are the
        # rejection that never says no. Added by the recall audit.
        "recall-audit",
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
    _phrase(
        "acknowledged",
        0.94,
        r"\b(?:confermiamo\s+di\s+aver\s+ricevuto\s+(?:la\s+tua\s+candidatura|il\s+tuo\s+cv)"
        r"|thanks\s+for\s+taking\s+the\s+time\s+to\s+apply)\b",
        "recall-audit",
    ),
    # Invitations.
    _phrase(
        "interview_invite",
        0.9,
        r"\b(?:would\s+like\s+to\s+invite\s+you|invite\s+you\s+to\s+(?:an?\s+)?(?:interview|call)"
        r"|schedule\s+(?:a|an)\s+(?:call|interview|chat)"
        r"|next\s+step\s+in\s+(?:the|our)\s+process"
        r"|you(?:'|’)?re\s+invited\s+to\s+an\s+interview)\b",
        "recall-audit",
    ),
    _phrase(
        "interview_invite",
        0.9,
        r"\b(?:vorremmo\s+invitarti|fissare\s+un\s+colloquio"
        r"|organizzare\s+un(?:\s+breve)?\s+(?:colloquio|call)"
        r"|disponibilità\s+per\s+un\s+colloquio"
        r"|(?:le|vi|ti)\s+confermiamo\s+il\s+colloquio)\b",
        "recall-audit",
    ),
    # Arranging the first call.
    #
    # The vocabulary had no phrase for this intent at all, and it is the single
    # commonest thing an Italian recruiter writes: the whole negotiation of when
    # to speak happens in prose, over several short messages, none of which say
    # "colloquio". Ten of the twenty-four signals a recall audit found the
    # deterministic ladder missing were this.
    _phrase(
        "schedule_screening",
        0.9,
        r"\b(?:(?:call|colloquio|incontro)\s+conoscitiv[oa]"
        r"|ti\s+confermo\s+(?:allora\s+)?la\s+nostra\s+(?:breve\s+)?(?:chiamata|call)"
        r"|opzioni\s+per\s+programmare|ecco\s+le\s+disponibilit[àa]"
        r"|saresti\s+disponibile\s+per\s+una\s+(?:breve\s+)?(?:chiamata|call))\b",
        "recall-audit",
    ),
    _phrase(
        "schedule_screening",
        0.88,
        # "ti contatterò domani alle ore 10:15" · "Potrebbe andare bene domani
        # 28/05 alle ore 10?" — a weekday and a time, in a sentence about
        # speaking. Both halves are required: a date alone is any mail at all.
        r"(?:ti\s+contatter[òo]|andare\s+bene|sentirci|risentirci|chiamarti)"
        r"[^.\n]{0,60}?\b(?:domani|luned[ìi]|marted[ìi]|mercoled[ìi]|gioved[ìi]|venerd[ìi]"
        r"|\d{1,2}[/.]\d{1,2})\b[^.\n]{0,40}?\bore\s+\d{1,2}",
        "recall-audit",
    ),
    _phrase(
        "schedule_screening",
        0.9,
        r"\bI\s+just\s+(?:re)?scheduled\s+the\s+"
        r"(?:meeting|call|interview|tech(?:nical)?\s+interview)\b",
        "recall-audit",
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
