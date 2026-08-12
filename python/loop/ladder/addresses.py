"""Reading an address out of a From header.

`domain_of_address` in the domain answers the question the classifier asks — who
sent this, at the level of an organisation. This answers the one the ladder asks
about a single mailbox: is this address the user's own.
"""

import re

_ANGLED = re.compile(r"<([^<>]+)>")
_BARE = re.compile(r"[^\s<>,;]+@[^\s<>,;]+")


def address_of(header: str) -> str | None:
    """The address itself, lower-cased, display name discarded."""
    angled = _ANGLED.search(header)
    candidate = (
        angled.group(1) if angled else (m.group(0) if (m := _BARE.search(header)) else "")
    )
    return candidate.strip().lower() or None
