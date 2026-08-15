# Connecting a Google mailbox

This is the one step nobody can do for you: the OAuth client belongs to your
Google account. It takes about ten minutes and costs nothing.

Until it exists, everything else works — `cd backend && uv run python scripts/e2e.py` runs the whole pipeline
against a stub mailbox, and quick-add covers applications by hand.

---

## 1 · A project and the two APIs

1. Open [console.cloud.google.com](https://console.cloud.google.com) and create
   a project. Call it whatever you like; nobody else will see it.
2. **APIs & Services → Library**, enable:
   - Gmail API
   - Google Calendar API

## 2 · The consent screen

**APIs & Services → OAuth consent screen**

- User type: **External**. (Internal needs a Workspace domain.)
- App name: Loop. Support e-mail: yours.
- **Scopes**: add exactly these two, and nothing else.
  - `https://www.googleapis.com/auth/gmail.readonly`
  - `https://www.googleapis.com/auth/calendar.readonly`
- **Test users**: add your own address.

Leave it in *Testing*. It stays unverified, which is why the onboarding flow
shows you Google's unverified-app warning *before* you meet it — an unexplained
scary screen is how people abandon a setup halfway through. Verification and a
CASA assessment are only needed to open this to strangers, which is phase 4.

A token issued to a Testing-mode app expires after seven days. For a
single-tenant box that means re-consenting weekly, so once the setup works,
publish the app (**Publishing status → Publish app**) and accept the unverified
warning permanently. Refresh tokens then last until you revoke them.

## 3 · The client

**APIs & Services → Credentials → Create credentials → OAuth client ID**

- Application type: **Web application**
- Authorised redirect URI: `http://localhost:3000/api/mailboxes/gmail/callback`
  for development, and `https://your-domain/api/mailboxes/gmail/callback` for
  the real deployment.

Copy the client id and secret into `.env`:

```
GOOGLE_CLIENT_ID=…apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=…
GOOGLE_REDIRECT_URI=http://localhost:3000/api/mailboxes/gmail/callback
```

Restart the gateway and connect from the onboarding flow.

---

## 4 · Push notifications (optional)

Without a Pub/Sub topic the connector polls every five minutes, which is fine
for a personal box. With one, new mail arrives within a second.

1. **Pub/Sub → Create topic**, e.g. `loop`.
2. On that topic, grant `gmail-api-push@system.gserviceaccount.com` the
   **Pub/Sub Publisher** role. Gmail will refuse to create a watch otherwise.
3. **Create subscription** → type **Push**, endpoint
   `https://your-domain/api/gmail/push`, with **authentication enabled** (an
   OIDC token). The gateway verifies the Google-signed JWT and ignores the
   payload entirely — the connector re-reads history from its own cursor, so a
   forged notification can at most cause one extra sync.
4. Set `GOOGLE_PUBSUB_TOPIC=projects/<project>/topics/loop`.

The push endpoint needs a public HTTPS URL. On a box without one, a Cloudflare
tunnel works; or leave the topic unset and let it poll.

---

## What Loop can and cannot do with this grant

**It can** read message headers and bodies to find applications, read calendar
events to detect interviews, and keep the fields it extracts — company, role,
stage, dates.

**It cannot** send, reply, delete or label anything; store your email bodies,
which live in memory for one parse; or reach your contacts, Drive, or anything
outside mail and calendar.

The refresh token is sealed with a per-user data key, which is itself sealed
with a key that lives outside the database. A stolen dump yields nothing
readable. You can revoke the grant at any time from
[myaccount.google.com/permissions](https://myaccount.google.com/permissions) —
Loop will show failure state F1 and keep every application it already has.
