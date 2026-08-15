"""Sender domains the ladder judges by, in one place.

In the TypeScript these lists were private to the classifier, so rung 2 could
not see them — which is why a coding-practice newsletter reached the
deterministic vocabulary pass and was filed as a take-home. A domain judgement
is one fact about a sender and every rung that reads senders needs it.
"""

from collections.abc import Iterable

from loop.domain import matches_domain_suffix

# Bulk-flagged but relevant. "The single most common false-negative in the whole
# system; there is a fixture for it." Their confirmations carry
# List-Unsubscribe and Precedence: bulk exactly like their job alerts do, so the
# penalty is waived before it applies rather than compensated afterwards.
BULK_WHITELIST: tuple[str, ...] = (
    "linkedin.com",
    "e.linkedin.com",
    "bounce.linkedin.com",
    "indeed.com",
    "match.indeed.com",
    "indeedemail.com",
)

SOCIAL_NOISE: tuple[str, ...] = (
    "facebook.com",
    "facebookmail.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "github.com",
    "gitlab.com",
    "notifications.google.com",
    "youtube.com",
    "medium.com",
    "substack.com",
    "meetup.com",
    "slack.com",
    "discord.com",
    "reddit.com",
    "quora.com",
    "pinterest.com",
    "tiktok.com",
)

MEETING_HOSTS: tuple[str, ...] = (
    "meet.google.com",
    "zoom.us",
    "teams.microsoft.com",
    "teams.live.com",
    "whereby.com",
    "meet.jit.si",
    "webex.com",
    "gotomeeting.com",
    "around.co",
)

PERSONAL_MAIL: tuple[str, ...] = (
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "hotmail.it",
    "live.com",
    "yahoo.com",
    "yahoo.it",
    "icloud.com",
    "me.com",
    "proton.me",
    "protonmail.com",
    "libero.it",
    "virgilio.it",
    "alice.it",
    "tiscali.it",
    "fastwebnet.it",
    "tin.it",
    "gmx.de",
    "web.de",
)

# Practice sites and course vendors sell using the vocabulary of hiring: a
# LeetCode promotion says "coding challenge" in the same words a real take-home
# does. They never send an application, so the deterministic vocabulary pass
# does not read them at all.
LEARNING_PLATFORMS: tuple[str, ...] = (
    "leetcode.com",
    "hackerrank.com",
    "codewars.com",
    "codingame.com",
    "coursera.org",
    "udemy.com",
    "udacity.com",
    "datacamp.com",
    "educative.io",
    "algoexpert.io",
    "pluralsight.com",
    "codecademy.com",
    "kaggle.com",
)


def in_list(domain: str | None, candidates: Iterable[str]) -> bool:
    if domain is None:
        return False
    return any(matches_domain_suffix(domain, c) for c in candidates)


def names_an_employer(domain: str | None) -> bool:
    """Whether a company name may be derived from this domain.

    A personal mailbox, a social network and a meeting host all send mail about
    applications and none of them is the employer.
    """
    if not domain:
        return False
    return not in_list(
        domain, (*PERSONAL_MAIL, *SOCIAL_NOISE, *MEETING_HOSTS, *LEARNING_PLATFORMS)
    )
