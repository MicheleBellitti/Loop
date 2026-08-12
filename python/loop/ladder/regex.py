"""Compiling the patterns that live in `rules/ats/*.yaml`.

Those files are data shared with the TypeScript implementation and their
patterns are written in JavaScript regex syntax, which differs from Python's in
exactly one way that matters here: a named group is `(?<name>…)` rather than
`(?P<name>…)`. Translating at compile time is what lets one set of rule files
serve both implementations while the port is being validated.

Two differences are left alone deliberately:

  · `$` without the multiline flag matches before a trailing newline in Python
    and only at the very end in JavaScript. Subjects are stripped and bodies are
    matched with explicit anchors, so this has no effect on the corpus; making
    it exact would mean rewriting `$` as `\\Z` inside patterns the rule author
    reads and edits, which trades a real cost for a theoretical one.

  · `\\w` and `\\b` are Unicode-aware in Python and ASCII-only in JavaScript.
    That difference favours Python on Italian mail, which is most of this
    mailbox, so it is kept.
"""

import re

# `(?<` that is not the start of a lookbehind assertion.
_JS_NAMED_GROUP = re.compile(r"\(\?<(?![=!])")


def compile_js(pattern: str, flags: int = re.IGNORECASE) -> re.Pattern[str]:
    return re.compile(_JS_NAMED_GROUP.sub("(?P<", pattern), flags)


def first_group(pattern: re.Pattern[str], text: str, name: str) -> str | None:
    """The named group of the first match, treating empty as absent."""
    match = pattern.search(text)
    if match is None:
        return None
    return (match.groupdict().get(name) or "").strip() or None
