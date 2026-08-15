"""Web Push, behind a protocol.

This is the only outbound network call in the product. It sends notifications
and it will never send mail: the whole design rests on the mailbox being read
and not written to, and the absence of a send path is what makes that checkable
rather than promised.

The protocol below is the point of this module. Every rule the notifier applies
— the daily cap, the quiet window, which rules may interrupt at all — is a
refusal to send, and a refusal is only testable if sending is something that can
be substituted. So `PushSender` is what the service depends on, and the real
implementation is one of its two members.
"""

import asyncio
from dataclasses import dataclass
from typing import Final, Literal, Protocol

# Twelve hours. A push that could not be delivered while the phone was off is
# still worth showing this evening and is noise tomorrow morning.
PUSH_TTL_SECONDS: Final = 12 * 3600

PushResult = Literal["ok", "gone", "failed", "unconfigured"]

# What the endpoint says when the browser has thrown the subscription away.
_SUBSCRIPTION_IS_GONE = frozenset({404, 410})


@dataclass(frozen=True, slots=True)
class VapidConfig:
    """The keypair that identifies this server to a push service.

    Either half missing means every send returns `unconfigured` rather than
    failing, so a deployment without keys is a product with no notifications
    rather than a service that crashes on the first suggestion.
    """

    public_key: str | None = None
    private_key: str | None = None
    subject: str = "mailto:loop@localhost"

    @property
    def configured(self) -> bool:
        return bool(self.public_key and self.private_key)


@dataclass(frozen=True, slots=True)
class PushSubscription:
    endpoint: str
    p256dh: str
    auth: str


@dataclass(frozen=True, slots=True)
class PushPayload:
    """The four keys the service worker reads, and it reads no others.

    `title` is positional in `showNotification` and an absent one renders the
    string "undefined" on the lock screen. `tag` is what collapses a redelivery
    into the notification already showing instead of buzzing a second time —
    which matters because delivery here is at-least-once and the push happens
    before the row that records it.
    """

    title: str
    body: str
    url: str
    tag: str

    def as_json(self) -> dict[str, str]:
        return {"title": self.title, "body": self.body, "url": self.url, "tag": self.tag}


class PushSender(Protocol):
    async def send(
        self, subscription: PushSubscription, payload: PushPayload
    ) -> PushResult: ...


class WebPushSender:
    """The real one. `pywebpush` owns the cryptography.

    Everything protocol-level — the ECDH agreement against the subscription's
    key, HKDF, the aes128gcm body, the ES256 VAPID assertion — is the library's,
    and reimplementing any of it here would be writing cryptography to save a
    dependency. It is synchronous, so it runs in a thread.
    """

    def __init__(self, vapid: VapidConfig) -> None:
        self._vapid = vapid

    async def send(
        self, subscription: PushSubscription, payload: PushPayload
    ) -> PushResult:
        if not self._vapid.configured:
            return "unconfigured"
        return await asyncio.to_thread(self._send, subscription, payload)

    def _send(self, subscription: PushSubscription, payload: PushPayload) -> PushResult:
        import json

        from pywebpush import WebPushException, webpush

        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=json.dumps(payload.as_json()),
                vapid_private_key=self._vapid.private_key,
                vapid_claims={"sub": self._vapid.subject},
                ttl=PUSH_TTL_SECONDS,
            )
        except WebPushException as error:
            status = getattr(error.response, "status_code", None)
            return "gone" if status in _SUBSCRIPTION_IS_GONE else "failed"
        except OSError:
            # A DNS failure or a dropped connection. Nothing is retried and the
            # notification is lost — which is the reference's behaviour, and is
            # defensible only because the same suggestion is still on the card.
            return "failed"
        return "ok"
