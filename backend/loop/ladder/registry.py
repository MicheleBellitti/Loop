"""Rung 1's template registry: one declarative YAML file per ATS vendor.

"One rule covers thousands of companies." Rules are data, reviewed like code —
every pattern ships with a fixture and CI runs the whole corpus on every commit.

Pydantic replaces zod as the schema, with two things tightened on the way:
unknown keys are rejected rather than silently dropped, and a pattern that
matches on neither subject nor body is an error rather than a line the matcher
skips at run time. Both were failures a rule author could not see.
"""

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from loop.domain import matches_domain_suffix
from loop.domain.messages import Intent
from loop.paths import rules_dir

from .regex import compile_js

# `other` lets a rule say "this vendor sent this, and it is not about an
# application" — webinar invitations, job alerts, newsletters. Without it those
# fall through to the model, which is asked to guess about mail a rule already
# recognises perfectly well as noise.
RuleIntent = Literal[
    "applied",
    "acknowledged",
    "schedule_screening",
    "interview_invite",
    "interview_cancelled",
    "take_home",
    "rejected",
    "offer",
    "negotiation",
    "other",
]

Locale = Literal["it", "en"]
_DEFAULT_LOCALE: list[Locale] = ["en"]

# Where the employer's name comes from.
#
# `sender_display_name` is the reliable one and it is the default for every ATS,
# because an ATS sends *on behalf of* the employer and puts the employer in the
# From display name. Subject lines put whatever they like in that slot — Lever's
# "Thanks for applying to Machine Learning Engineer, here is a link to manage
# your application data" has the role where the company would be, and a subject
# capture there produced an application filed under a sentence fragment.
CompanySource = Literal[
    "sender_display_name", "subject_capture", "sender_domain", "body_capture"
]

JsRegex = Annotated[str, Field(min_length=1)]


class RulesError(Exception):
    """A rule file that cannot be trusted. Never swallowed: rung 1 is the rung
    that covers thousands of companies, and a silently skipped file is a silently
    unread mailbox."""


class PatternSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: RuleIntent
    confidence: Annotated[float, Field(ge=0, le=1)]
    subject: JsRegex | None = None
    body: JsRegex | None = None
    # Named capture groups feed the extracted fields.
    extract: Mapping[str, JsRegex] = Field(default_factory=dict)
    # Restrict this pattern to one language when the vendor sends both.
    locale: Locale | None = None

    @model_validator(mode="after")
    def _must_match_something(self) -> Self:
        if self.subject is None and self.body is None:
            raise ValueError("a pattern needs a subject or a body to match on")
        return self


class MatchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sender_domains: Annotated[list[str], Field(min_length=1)]
    # A value of null means "the header must simply be present".
    headers: Mapping[str, str | None] = Field(default_factory=dict)


class TestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture: str
    expect: Mapping[str, object]


class RuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vendor: str
    match: MatchSpec
    patterns: Annotated[list[PatternSpec], Field(min_length=1)]
    locale: list[Locale] = Field(default_factory=_DEFAULT_LOCALE.copy)
    company_from: CompanySource = "sender_display_name"
    tests: list[TestSpec] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Pattern:
    """One template, with its regexes compiled once."""

    intent: Intent
    confidence: float
    locale: Locale | None
    subject: re.Pattern[str] | None
    body: re.Pattern[str] | None
    extract: Mapping[str, re.Pattern[str]]

    @classmethod
    def from_spec(cls, spec: PatternSpec) -> Self:
        return cls(
            intent=spec.intent,
            confidence=spec.confidence,
            locale=spec.locale,
            subject=compile_js(spec.subject) if spec.subject else None,
            body=compile_js(spec.body) if spec.body else None,
            extract={name: compile_js(p) for name, p in spec.extract.items()},
        )


@dataclass(frozen=True, slots=True)
class Rule:
    """One vendor."""

    vendor: str
    sender_domains: tuple[str, ...]
    headers: Mapping[str, str | None]
    locale: tuple[Locale, ...]
    company_from: CompanySource
    patterns: tuple[Pattern, ...]

    @classmethod
    def from_spec(cls, spec: RuleSpec) -> Self:
        return cls(
            vendor=spec.vendor,
            sender_domains=tuple(spec.match.sender_domains),
            headers=dict(spec.match.headers),
            locale=tuple(spec.locale),
            company_from=spec.company_from,
            patterns=tuple(Pattern.from_spec(p) for p in spec.patterns),
        )

    def sends_from(self, domain: str) -> bool:
        return any(matches_domain_suffix(domain, d) for d in self.sender_domains)


class RuleRegistry:
    """Every vendor rule, loaded and compiled.

    An object rather than the module-level cache the TypeScript used: that cache
    needed a reset hook that only the tests called, which is the shape of a
    global pretending not to be one.
    """

    def __init__(self, rules: Sequence[Rule]) -> None:
        self._rules = tuple(rules)
        self.ats_domains = tuple(
            dict.fromkeys(d for r in self._rules for d in r.sender_domains)
        )

    def __iter__(self) -> Iterator[Rule]:
        return iter(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def rule_for(self, domain: str | None) -> Rule | None:
        if not domain:
            return None
        return next((r for r in self._rules if r.sends_from(domain)), None)

    def vendor_for(self, domain: str | None) -> str | None:
        rule = self.rule_for(domain)
        return rule.vendor if rule else None

    def is_ats(self, domain: str | None) -> bool:
        return self.rule_for(domain) is not None

    @classmethod
    def load(cls, directory: Path | None = None) -> Self:
        source = directory or default_rules_dir()
        files = sorted(p for p in source.iterdir() if p.suffix in {".yaml", ".yml"})
        if not files:
            raise RulesError(f"no rule files in {source}")
        return cls([_read(path) for path in files])


def _read(path: Path) -> Rule:
    try:
        spec = RuleSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
        )
        raise RulesError(f"{path.name} is invalid: {problems}") from error
    except yaml.YAMLError as error:
        raise RulesError(f"{path.name} is not valid YAML: {error}") from error
    return Rule.from_spec(spec)


def default_rules_dir() -> Path:
    """`rules/ats/` at the repository root.

    The rule files are shared with the TypeScript implementation while both run,
    so they are deliberately not copied under `python/`. They move here when the
    TypeScript is retired; `LOOP_RULES_DIR` covers anything in between.
    """
    return rules_dir()
