"""The follow-up draft, which the product will not send for you.

Loop holds a read-only mailbox scope and that is the whole point: a tracker that
can send mail is a tracker you have to trust with your correspondence. So this
composes the message and hands it over as a `mailto:` — the send button is your
mail client's, and `can_send` is false on every path there is.

The copy is deliberately dull. A follow-up that reads as though software wrote
it is worse than no follow-up, so this writes the two sentences anyone would
write and leaves the rest of the message to the person sending it.
"""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

Language = Literal["it", "en", "other"]


@dataclass(frozen=True, slots=True)
class Draft:
    subject: str
    body: str
    mailto_url: str
    # Always false. There is no code path that makes it true, and there is not
    # meant to be one.
    can_send: bool
    note: str


_CANNOT_SEND = "Loop holds a read-only scope, so it cannot send this."


def build_draft(
    *,
    company: str,
    language: Language,
    last_event: str | None,
    thread_id: str | None,
) -> Draft:
    italian = language == "it"
    subject = f"Re: candidatura {company}" if italian else f"Re: {company} application"

    if last_event:
        # Lower-cased because it lands mid-sentence: "thanks again for the
        # technical interview."
        context = (
            f"grazie ancora per {last_event.lower()}."
            if italian
            else f"thanks again for the {last_event.lower()}."
        )
    else:
        context = "grazie ancora per il tempo dedicato." if italian else (
            "thanks again for your time."
        )

    body = (
        f"Ciao,\n\n{context}\nServe qualcosa da parte mia mentre il team decide?"
        "\n\nUn saluto,"
        if italian
        else f"Hi,\n\n{context}\nIs there anything you need from my side while "
        "the team decides?\n\nBest,"
    )
    return Draft(
        subject=subject,
        body=body,
        mailto_url=_mailto(subject, body, thread_id),
        can_send=False,
        note=_CANNOT_SEND,
    )


def _mailto(subject: str, body: str, thread_id: str | None) -> str:
    """Percent-encoded, not form-encoded.

    A `mailto:` query is not `application/x-www-form-urlencoded`, so the
    reference's `URLSearchParams` turned every space into a `+` and the subject
    arrived in Apple Mail as `Re:+Acme+application`.

    The recipient is empty because nothing in this schema stores the address a
    thread came from — no table holds message text or headers past processing.
    Your mail client fills it in when you reply.
    """
    parts = [f"subject={quote(subject)}", f"body={quote(body)}"]
    if thread_id:
        parts.append(f"In-Reply-To={quote(f'<{thread_id.strip('<>')}>')}")
    return "mailto:?" + "&".join(parts)
