-- 018 · enrolling a passkey and signing in stop sharing one challenge slot
--
-- `auth_secrets` had a single `webauthn_challenge`, and both flows wrote it.
-- `/api/auth/register/options` needs a session; `/api/auth/login/options` is
-- public and must be — it is the screen you see before you are anybody — so an
-- unauthenticated request could overwrite a challenge an enrolment was in the
-- middle of. The authenticator then returns a signature over a challenge the
-- server has already replaced, `register/verify` answers `no_challenge`, and
-- the user is told to request options again — which they do, into the same
-- race. Nothing has to be malicious for this: a second tab sitting on the sign
-- in screen polls it, and the two flows are exactly the ones a person runs
-- together when adding a passkey to an account they are already signed into.
--
-- One column each. The one-shot semantics are unchanged and so are the reads:
-- the CTE that consumes a challenge in the same statement it reads it stays
-- word for word, once per flow.

alter table auth_secrets
  add column if not exists registration_challenge text,
  add column if not exists registration_expires_at timestamptz;
