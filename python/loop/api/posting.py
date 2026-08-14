"""Fetching a URL the user pasted, which is the one place this becomes a client.

Quick add accepts a link to a job posting and reads it. That turns a server
sitting inside a private network into an open-ended HTTP client pointed at
whatever a request asks for, and the defence is stated in the spec: resolve DNS
first, refuse private ranges, refuse more than three redirects, refuse anything
that is not HTML.

The reference's own comment claims the request is then made "against the
resolved address, so a name that answers publicly on the first lookup and
privately on the second cannot slip through". Its code passes the hostname to
`fetch`, which resolves it a second time — so the window it describes was open.
Here the connection really is made to the address that was checked, with the
hostname carried in `Host` and in the TLS handshake.

Everything in here is best-effort by contract: quick add creates the application
whether or not any of this works, and a failure is logged rather than returned.
"""

import asyncio
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_REDIRECTS: Final = 3
MAX_BYTES: Final = 512 * 1024
TIMEOUT_SECONDS: Final = 8.0

_HTML = re.compile(r"^(?:text/html|application/xhtml)", re.I)

_ATS_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"greenhouse\.io|greenhouse-mail", re.I), "greenhouse"),
    (re.compile(r"lever\.co", re.I), "lever"),
    (re.compile(r"myworkday|workday", re.I), "workday"),
    (re.compile(r"ashbyhq", re.I), "ashby"),
    (re.compile(r"smartrecruiters", re.I), "smartrecruiters"),
    (re.compile(r"workable", re.I), "workable"),
    (re.compile(r"icims", re.I), "icims"),
    (re.compile(r"taleo|oraclecloud", re.I), "taleo"),
    (re.compile(r"recruitee", re.I), "recruitee"),
    (re.compile(r"bamboohr", re.I), "bamboohr"),
)

_JSON_LD = re.compile(
    r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.I | re.S
)
_TITLE = re.compile(r"<title>([^<]+)</title>", re.I)


class BlockedUrl(Exception):
    """The URL was refused before anything was sent to it."""


@dataclass(frozen=True, slots=True)
class Posting:
    company: str | None = None
    role: str | None = None
    location: str | None = None
    ats_vendor: str | None = None
    comp: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _Target:
    """A URL and the address it actually resolved to."""

    url: str
    host: str
    address: str


def is_public(address: str) -> bool:
    """Whether an address belongs to the internet rather than to this network.

    `ipaddress` classifies loopback, link-local, private, reserved and multicast
    for both families, which is the whole list the reference spelled out by
    hand — including 169.254.0.0/16, where every cloud metadata service lives.
    Carrier-grade NAT is the one range it does not call private, so it is named.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def resolve_public(raw: str) -> _Target:
    """Refuse the URL, or return it alongside one address known to be public.

    *Every* address the name resolves to has to be public, not just the first:
    a name that answers with a public address and a private one is a name that
    will eventually be connected to privately.
    """
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise BlockedUrl("only http and https are fetched")
    host = (parts.hostname or "").strip("[]")
    if not host:
        raise BlockedUrl("not a URL")

    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = await _lookup(host)

    for address in addresses:
        if not is_public(address):
            raise BlockedUrl(f"refuses to fetch a private address ({address})")
    return _Target(url=raw, host=host, address=addresses[0])


async def _lookup(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except OSError as error:
        raise BlockedUrl("host does not resolve") from error
    if not infos:
        raise BlockedUrl("host does not resolve")
    return [str(info[4][0]) for info in infos]


async def fetch_posting(raw: str) -> tuple[str, str]:
    """The final URL and its HTML, or `BlockedUrl`.

    Redirects are followed by hand so every hop is resolved and checked again —
    an open redirect on a public host is otherwise a way to reach a private one.
    """
    import httpx

    target = await resolve_public(raw)
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, follow_redirects=False, verify=True
    ) as client:
        for hop in range(MAX_REDIRECTS + 1):
            # Streamed, so the headers can be judged before a byte of the body
            # is kept and the read can be abandoned at the cap. A plain `get`
            # buffers the whole response first and applies `MAX_BYTES` to what
            # is already in memory, which makes the limit a slice of a download
            # nobody bounded — one pasted link to a large file and the gateway
            # holds all of it.
            async with client.stream(
                "GET",
                _to_address(target),
                headers={
                    "host": target.host,
                    "accept": "text/html",
                    "user-agent": "Loop/1.0 (+self-hosted application tracker)",
                },
                extensions={"sni_hostname": target.host},
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise BlockedUrl("redirect without a location")
                    if hop == MAX_REDIRECTS:
                        raise BlockedUrl("too many redirects")
                    target = await resolve_public(urljoin(target.url, location))
                    continue

                content_type = response.headers.get("content-type", "")
                if not _HTML.match(content_type):
                    raise BlockedUrl(
                        f"refuses a non-HTML response ({content_type or 'unknown'})"
                    )
                return target.url, await _read_capped(response)
    raise BlockedUrl("too many redirects")


async def _read_capped(response: Any) -> str:
    """At most `MAX_BYTES`, decoded however the response says it is encoded.

    The cap is on bytes and not on characters: the point is what this process
    agrees to hold, and a multi-byte encoding makes those two very different
    numbers.
    """
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) >= MAX_BYTES:
            break
    return bytes(body[:MAX_BYTES]).decode(
        response.charset_encoding or "utf-8", errors="replace"
    )


def _to_address(target: _Target) -> str:
    """The same URL with the checked address in place of the name."""
    parts = urlsplit(target.url)
    host = f"[{target.address}]" if ":" in target.address else target.address
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def parse_posting(url: str, html: str) -> Posting:
    """Structured data first, then Open Graph, then the title. Never invents."""
    company = role = location = None
    comp = None

    structured = _structured_data(html)
    if structured:
        role = _text(structured.get("title"))
        company = _text(_nested(structured, "hiringOrganization", "name"))
        location = _text(_nested(structured, "jobLocation", "address", "addressLocality"))
        comp = _salary(structured)

    role = role or _meta(html, "og:title") or _title(html)
    company = company or _meta(html, "og:site_name")
    vendor = next(
        (name for pattern, name in _ATS_HINTS if pattern.search(url) or pattern.search(html)),
        None,
    )
    return Posting(company, role, location, vendor, comp)


def _structured_data(html: str) -> dict[str, Any] | None:
    """A `ld+json` block that does not parse is simply absent."""
    match = _JSON_LD.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def _salary(posting: dict[str, Any]) -> dict[str, Any] | None:
    value = _nested(posting, "baseSalary", "value")
    minimum = value.get("minValue") if isinstance(value, dict) else None
    if minimum is None:
        return None
    maximum = value.get("maxValue") if isinstance(value, dict) else None
    base = posting.get("baseSalary")
    currency = base.get("currency") if isinstance(base, dict) else None
    return {
        "min_minor": round(float(minimum) * 100),
        "max_minor": round(float(maximum) * 100) if maximum else None,
        "currency": str(currency or "EUR"),
    }


def _nested(data: dict[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _meta(html: str, prop: str) -> str | None:
    pattern = re.compile(
        rf"<meta[^>]+(?:property|name)=[\"']{re.escape(prop)}[\"'][^>]+content=[\"']([^\"']+)[\"']",
        re.I,
    )
    match = pattern.search(html)
    return match.group(1) if match else None


def _title(html: str) -> str | None:
    match = _TITLE.search(html)
    return match.group(1).strip() if match else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
