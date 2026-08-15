"""Normalisation used by the resolver.

Kept pure and here so the golden corpus can test it without a database.
Engineering Spec §09 specifies the behaviour in one line each; this is that
line made explicit.

A note for the port: once spaCy is in place (`it_core_news_lg`), company and
role extraction should move to NER and these regexes become the fallback rather
than the mechanism. They are carried over because they are the current
behaviour and the differential harness needs something to diff against — not
because they are the destination.
"""

import re
import unicodedata
from dataclasses import dataclass

from .types import WorkMode

# Legal forms, stripped so "Nexi S.p.A." and "Nexi" are one company.
_LEGAL_SUFFIXES = [
    "s.r.l.s.",
    "s.r.l.",
    "srls",
    "srl",
    "s.p.a.",
    "spa",
    "s.a.s.",
    "sas",
    "s.n.c.",
    "snc",
    "s.a.p.a.",
    "sapa",
    "gmbh",
    "mbh",
    "ag",
    "kg",
    "ohg",
    "ug",
    "inc.",
    "inc",
    "llc",
    "l.l.c.",
    "ltd.",
    "ltd",
    "limited",
    "plc",
    "corp.",
    "corp",
    "corporation",
    "company",
    "co.",
    "b.v.",
    "bv",
    "n.v.",
    "nv",
    "s.a.",
    "sa",
    "s.à.r.l.",
    "sarl",
    "oy",
    "oyj",
    "ab",
    "a/s",
    "as",
    "aps",
    "kft",
    "zrt",
    "d.o.o.",
    "doo",
    "pte",
    "pty",
    "sp. z o.o.",
    "sp z oo",
]

# Deliberately excluded from the list above: "group" and "holding". They are
# frequently part of the name a company actually trades under, and stripping
# them would merge two different legal entities in the same family.

_SENIORITY_WORDS: dict[str, str] = {
    "jr": "junior",
    "jr.": "junior",
    "junior": "junior",
    "grad": "junior",
    "graduate": "junior",
    "entry": "junior",
    "intern": "intern",
    "internship": "intern",
    "trainee": "intern",
    "mid": "mid",
    "middle": "mid",
    "mid-level": "mid",
    "sr": "senior",
    "sr.": "senior",
    "senior": "senior",
    "snr": "senior",
    "staff": "staff",
    "principal": "principal",
    "lead": "lead",
    "head": "head",
    "director": "director",
    "vp": "vp",
    "chief": "chief",
    "i": "mid",
    "ii": "mid",
    "iii": "senior",
    "iv": "senior",
}

# Abbreviations the spec names, plus the ones that appear in the same mail.
_EXPANSIONS: dict[str, str] = {
    "sr": "senior",
    "jr": "junior",
    "eng": "engineer",
    "engr": "engineer",
    "engineering": "engineer",
    "dev": "developer",
    "devel": "developer",
    "be": "backend",
    "back-end": "backend",
    "fe": "frontend",
    "front-end": "frontend",
    "fs": "fullstack",
    "full-stack": "fullstack",
    "swe": "software engineer",
    "sde": "software engineer",
    "sre": "site reliability engineer",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "qa": "quality assurance",
    "pm": "product manager",
    "po": "product owner",
    "ux": "user experience",
    "ui": "user interface",
    "db": "database",
    "k8s": "kubernetes",
    "infra": "infrastructure",
    "ops": "operations",
}

_CONTRACT_TERMS = [
    "full time",
    "full-time",
    "fulltime",
    "part time",
    "part-time",
    "parttime",
    "permanent",
    "temporary",
    "contract",
    "contractor",
    "freelance",
    "fixed term",
    "fixed-term",
    "internship",
    "apprenticeship",
    "tempo indeterminato",
    "tempo determinato",
    "stage",
    "tirocinio",
    "unbefristet",
    "festanstellung",
    "m/f/d",
    "f/m/d",
    "m/w/d",
    "w/m/d",
    "m/f/x",
    "d/f/m",
    "h/f",
    "m/f",
]

_WORK_MODES: list[tuple[re.Pattern[str], WorkMode]] = [
    (
        re.compile(r"\b(fully\s+)?remote\b|\bda\s+remoto\b|\bsmart\s*working\b|\bwfh\b", re.I),
        "remote",
    ),
    (re.compile(r"\bhybrid\b|\bibrido\b", re.I), "hybrid"),
    (re.compile(r"\bon[-\s]?site\b|\bin[-\s]?office\b|\bin\s+sede\b", re.I), "onsite"),
]

# Small gazetteer — enough to recognise a trailing location, not a geocoder.
_PLACES = {
    "milan",
    "milano",
    "rome",
    "roma",
    "turin",
    "torino",
    "naples",
    "napoli",
    "bologna",
    "florence",
    "firenze",
    "venice",
    "venezia",
    "genoa",
    "genova",
    "bari",
    "palermo",
    "trento",
    "trieste",
    "padova",
    "padua",
    "verona",
    "brescia",
    "bergamo",
    "biassono",
    "berlin",
    "munich",
    "münchen",
    "hamburg",
    "frankfurt",
    "cologne",
    "köln",
    "stuttgart",
    "london",
    "manchester",
    "dublin",
    "edinburgh",
    "paris",
    "lyon",
    "toulouse",
    "madrid",
    "barcelona",
    "lisbon",
    "lisboa",
    "porto",
    "amsterdam",
    "rotterdam",
    "brussels",
    "zurich",
    "zürich",
    "geneva",
    "vienna",
    "wien",
    "prague",
    "warsaw",
    "stockholm",
    "copenhagen",
    "oslo",
    "helsinki",
    "tallinn",
    "vilnius",
    "riga",
    "bucharest",
    "sofia",
    "budapest",
    "athens",
    "istanbul",
    "krakow",
    "kraków",
    "italy",
    "italia",
    "germany",
    "deutschland",
    "france",
    "spain",
    "españa",
    "portugal",
    "netherlands",
    "belgium",
    "switzerland",
    "austria",
    "poland",
    "sweden",
    "denmark",
    "norway",
    "finland",
    "ireland",
    "uk",
    "united kingdom",
    "europe",
    "eu",
    "emea",
    "remote",
    "hybrid",
    "onsite",
    "on-site",
    "anywhere",
}

_SEPARATORS = re.compile(r"\s+[-–—|·]\s+|\s*[/,]\s*")
_NON_WORD = re.compile(r"[^\w\s.&+-]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def _strip_accents_and_punct(s: str) -> str:
    decomposed = unicodedata.normalize("NFKD", s)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _WHITESPACE.sub(" ", _NON_WORD.sub(" ", without_marks)).strip()


def normalise_company(raw: str) -> str:
    """Casefold, strip the legal suffix, collapse whitespace.

    This is the key `by_alias` looks up, so it must be stable: two spellings of
    one company have to land on the same string or the resolver creates a
    duplicate application.
    """
    s = _strip_accents_and_punct(raw.lower())
    # Suffixes can stack ("Foo Italia S.r.l."), so strip repeatedly.
    changed = True
    while changed:
        changed = False
        for suf in _LEGAL_SUFFIXES:
            pattern = re.compile(rf"(^|\s){re.escape(suf)}$", re.I)
            if pattern.search(s):
                s = pattern.sub("", s).strip()
                changed = True
    return _WHITESPACE.sub(" ", s.rstrip(" .&+-")).strip()


def company_key(raw: str) -> str:
    """The key two spellings of one employer have to agree on.

    `normalise_company` folds case, accents and legal suffixes, which is enough
    until the same company arrives by two routes: an ATS puts "ION Group" in
    the From display name, while the company's own mail resolves through its
    domain to "iongroup". Those differ by one space and became two companies,
    two pipelines and two sets of statistics.

    The key drops everything that is not a letter or a digit. It is only ever a
    lookup key — the human-readable `canonical_name` keeps its spaces.
    """
    return "".join(c for c in normalise_company(raw) if c.isalnum())


@dataclass(frozen=True, slots=True)
class NormalisedRole:
    # The comparison key — what gets embedded and compared by cosine.
    role: str
    # Pulled out of the title into its own field, as the spec requires.
    seniority: str | None
    location: str | None
    work_mode: WorkMode | None


def _looks_like_place(segment: str) -> bool:
    s = segment.strip().lower()
    if not s:
        return False
    if s in _PLACES:
        return True
    words = s.split()
    return len(words) <= 3 and any(w in _PLACES for w in words)


def _is_contract_term(segment: str) -> bool:
    s = segment.strip().lower()
    return any(s == t or t in s for t in _CONTRACT_TERMS)


def normalise_role(raw: str) -> NormalisedRole:
    """ "Senior Backend Engineer (m/f/d) - Milan, full time" → "backend engineer"."""
    work_mode: WorkMode | None = None
    for pattern, mode in _WORK_MODES:
        if pattern.search(raw):
            work_mode = mode
            break

    # Bracketed groups are almost always contract or diversity notation.
    bracketed: list[str] = []

    def _collect(m: re.Match[str]) -> str:
        bracketed.append(m.group(1))
        return " "

    s = re.sub(r"[(\[{]([^)\]}]*)[)\]}]", _collect, raw)

    location: str | None = None
    for b in bracketed:
        if _looks_like_place(b):
            location = b.strip()

    # Walk the separated segments and stop at the first that is a place or a
    # contract term — everything from there on is trailing metadata.
    segments = [seg.strip() for seg in _SEPARATORS.split(s) if seg.strip()]
    kept: list[str] = []
    for i, seg in enumerate(segments):
        if i > 0 and (_looks_like_place(seg) or _is_contract_term(seg)):
            if _looks_like_place(seg) and not location:
                location = seg
            break
        kept.append(seg)
    s = " ".join(kept)

    for t in _CONTRACT_TERMS:
        s = re.sub(rf"\b{re.escape(t)}\b", " ", s, flags=re.I)

    s = _strip_accents_and_punct(s.lower())

    seniority: str | None = None
    out: list[str] = []
    for tok in s.split():
        bare = tok.rstrip(".")
        sen = _SENIORITY_WORDS.get(bare)
        if sen:
            if not seniority:
                seniority = sen
            continue
        out.append(_EXPANSIONS.get(bare, bare))

    return NormalisedRole(
        role=_WHITESPACE.sub(" ", " ".join(out)).strip(),
        seniority=seniority,
        location=location,
        work_mode=work_mode,
    )


_ADDRESS = re.compile(r"<?([^<>\s]+)@([A-Za-z0-9.-]+)>?")


def domain_of_address(address: str) -> str | None:
    """`user@mail.company.com` → `mail.company.com`."""
    m = _ADDRESS.search(address.strip())
    return m.group(2).lower().rstrip(".") if m else None


def matches_domain_suffix(domain: str, candidate: str) -> bool:
    """`mail.greenhouse.io` matches `greenhouse.io`; `notgreenhouse.io` does not."""
    d, c = domain.lower(), candidate.lower()
    return d == c or d.endswith(f".{c}")
