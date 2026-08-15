"""The differences from the TypeScript that are meant to be there.

A port validated by diffing is only as good as its ability to tell a fix from a
mistake. Every deliberate change to the ladder is registered here with the
reason it was made; anything the table cannot explain is a porting error and the
diff fails on it.

Each predicate is written to match the one change it describes and nothing
adjacent. "Python found a company where the TypeScript did not" would forgive a
company the Python invented; "Python found the company the sender's own domain
names" forgives only the fallback that was fixed. Adding an entry is a decision,
not a way to quiet the harness.
"""

from collections.abc import Callable
from dataclasses import dataclass

from loop.domain import domain_of_address
from loop.ladder.company import company_from_domain
from loop.ladder.domains import LEARNING_PLATFORMS, in_list
from loop.ladder.phrases import match_phrase

from .corpus import BaselineCase, BaselineContext
from .runner import Verdict

Predicate = Callable[[BaselineCase, Verdict, BaselineContext], bool]


@dataclass(frozen=True, slots=True)
class Divergence:
    name: str
    note: str
    explains: Predicate


def _abstains_on_stage(before: BaselineCase, after: Verdict, _ctx: BaselineContext) -> bool:
    """§3.2 — an invitation whose title names no stage no longer guesses."""
    return (
        before.intent == after.intent
        and before.stage_hint == "technical"
        and after.stage_hint is None
    )


def _reads_a_known_thread(before: BaselineCase, after: Verdict, ctx: BaselineContext) -> bool:
    """A reply on an owned thread reached the vocabulary instead of a human.

    The TypeScript asserted thread identity as though it were an extraction and
    returned it before the phrases were tried, so a rejection arriving on a
    thread it already owned was never read at all.
    """
    thread = before.message.thread_id
    return (
        before.intent is None
        and after.rung == 2
        and thread is not None
        and thread in ctx.thread_to_application
    )


def _ignores_a_practice_site(
    before: BaselineCase, after: Verdict, _ctx: BaselineContext
) -> bool:
    """§3.4 — "coding challenge" in a LeetCode promotion is not a take-home."""
    sender = domain_of_address(before.message.headers.sender)
    return (
        before.intent == "take_home"
        and after.intent is None
        and in_list(sender, LEARNING_PLATFORMS)
    )


def _names_a_company_from_the_domain(
    before: BaselineCase, after: Verdict, _ctx: BaselineContext
) -> bool:
    """§3.3 — the domain fallback fires where the TypeScript's could not.

    `companyFromOrganiser` took an address and was called with a bare domain in
    the one place it mattered, so it always returned null there. Only forgiven
    when the name is the one that sender's own domain yields.
    """
    if before.intent != after.intent or before.company is not None or not after.company:
        return False
    invite = before.message.invite
    source = invite.organiser if invite else domain_of_address(before.message.headers.sender)
    return after.company == company_from_domain(source, ())


def _extends_the_vocabulary(
    before: BaselineCase, after: Verdict, _ctx: BaselineContext
) -> bool:
    """A phrase the TypeScript's vocabulary never had.

    Seven of the eleven phrases were added after the port, from a recall audit
    over this corpus: the whole `schedule_screening` intent, which an Italian
    recruiter negotiates over several short messages, none of them saying
    "colloquio". A message the reference could not place is only a porting error
    if the phrase that places it here was supposed to exist in both, so the
    phrase's own provenance decides.
    """
    if before.intent is not None or after.intent is None:
        return False
    phrase = match_phrase(f"{before.message.headers.subject}\n{before.message.text}")
    return phrase is not None and phrase.origin == "recall-audit"


def _waives_bulk_for_an_ats(
    before: BaselineCase, after: Verdict, _ctx: BaselineContext
) -> bool:
    """An ATS acknowledgement is no longer penalised for arriving in bulk.

    The waiver used to cover LinkedIn and Indeed by name. An applicant-tracking
    system sends in bulk because that is what it is, and the four points cost a
    real Italian acknowledgement its place in the queue.
    """
    return after.score == before.score + 4 and after.vendor is not None


DIVERGENCES: tuple[Divergence, ...] = (
    Divergence(
        "stage-abstention",
        "§3.2 abstain rather than default an untitled invite to technical",
        _abstains_on_stage,
    ),
    Divergence(
        "thread-vocabulary",
        "a known thread asserts identity, not intent, so the phrases still run",
        _reads_a_known_thread,
    ),
    Divergence(
        "practice-sites",
        "§3.4 practice sites are excluded from the deterministic phrase pass",
        _ignores_a_practice_site,
    ),
    Divergence(
        "company-from-domain",
        "§3.3 the domain fallback accepts a bare domain and now fires",
        _names_a_company_from_the_domain,
    ),
    Divergence(
        "vocabulary-extended",
        "a phrase added after the port, from the recall audit over this corpus",
        _extends_the_vocabulary,
    ),
    Divergence(
        "ats-bulk-waiver",
        "an ATS acknowledgement is no longer penalised for arriving in bulk",
        _waives_bulk_for_an_ats,
    ),
)

COMPARED_FIELDS = (
    "score",
    "outcome",
    "intent",
    "company",
    "role",
    "rung",
    "vendor",
    "stage_hint",
)


def differing_fields(before: BaselineCase, after: Verdict) -> tuple[str, ...]:
    return tuple(
        name for name in COMPARED_FIELDS if getattr(after, name) != getattr(before, name)
    )


def explain(before: BaselineCase, after: Verdict, ctx: BaselineContext) -> Divergence | None:
    return next((d for d in DIVERGENCES if d.explains(before, after, ctx)), None)
