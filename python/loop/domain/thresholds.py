"""Every tunable number in the system, in one place.

Engineering Spec §09: these are "tuned only against the golden corpus, and
changed only with the precision/recall table in the PR".

The prose travels with the numbers deliberately. A threshold without its reason
gets re-tuned by whoever touches it next, and several of these were derived
from measurements on a real mailbox rather than chosen — losing that provenance
would make them look arbitrary.
"""

from typing import Final

# ── Resolver · entity resolution (Spec §09) ─────────────────────────────────
#
# WARNING for the port: these were tuned against a *lexical hashing* embedder,
# not a real one. `LexicalEmbedder` was a stand-in for sentence-transformers,
# and carrying these numbers across unchanged once real embeddings are in place
# would be a silent regression. Re-tune against the corpus. decisions.md §3.3
# of the porting plan.

# One candidate application at this company: attach above this cosine.
ATTACH_SINGLE: Final = 0.72
# Several candidates: attach to the best only above this cosine…
ATTACH_MULTI: Final = 0.82
# …and only if it beats the runner-up by at least this much.
AMBIGUITY_MARGIN: Final = 0.05
# Cross-channel dedup: the same job found twice.
DEDUP_MERGE: Final = 0.93
# Below this confidence a signal never reaches the fold — it asks the human.
REVIEW_BELOW: Final = 0.60
# Two applications only merge if they were created within this window.
DEDUP_WINDOW_DAYS: Final = 14
# An automatic merge stays one tap from being undone for this long (D5).
MERGE_UNDO_DAYS: Final = 14

# The fold ignores anything below this. It is the same number as REVIEW_BELOW
# on purpose: a signal is either good enough to change your pipeline or good
# enough to be a question, never both and never neither.
FOLD_CONFIDENCE_FLOOR: Final = REVIEW_BELOW

# A human's confidence. Pins the field until another 1.0 event touches it.
PINNED_CONFIDENCE: Final = 1.0


# ── Display gates (Spec §11) ────────────────────────────────────────────────
# The client MUST honour these; below a gate it shows the count and names the
# threshold, which turns an empty chart into a progress bar rather than a
# disappointment.

# Ratios need this many closed applications before a percentage is honest.
RATIOS_MIN_CLOSED: Final = 8
# Between the two, the figure ships with a small-sample warning.
SMALL_SAMPLE_MAX: Final = 15
# Median dwell needs this many observed transitions.
TIME_IN_STAGE_MIN_TRANSITIONS: Final = 5
# Seasonal shape needs two quarters before it means anything.
SEASONAL_MIN_QUARTERS: Final = 2
# A channel row needs this many first-touch applications.
CHANNEL_MIN_APPLICATIONS: Final = 3

# Maturity exclusion. An application applied more recently than this and still
# sitting in `sent` has not had time to convert, so counting it drags every
# ratio down — the reason a naive funnel always looks like it is falling.
RATIO_MATURITY_DAYS: Final = 21


# ── Classifier (Spec §07) ───────────────────────────────────────────────────
# Biased towards recall, deliberately: dropping a real application is invisible
# and unrecoverable, while passing junk through costs four milliseconds.
CLASSIFIER_PASS: Final = 3
CLASSIFIER_CHEAP_ONLY: Final = 1


# ── Notification budget (Spec §12) ──────────────────────────────────────────
MAX_OPEN_SUGGESTIONS: Final = 3
MAX_PUSH_PER_DAY: Final = 1
# decisions.md C3 — the spec caps the daily push but never names the slot.
DAILY_SLOT: Final = "18:00"
QUIET_FROM: Final = "21:00"
QUIET_TO: Final = "08:00"
# The only alert allowed past the cap, and the §19 quiet-hours exception.
DEADLINE_EXEMPT_FROM_CAP: Final = True
DEADLINE_BREAKS_QUIET_HOURS: Final = True
# When a push fires. §12 gives exactly these two moments.
DEADLINE_WARN_HOURS: Final = (72, 12)
# When the *card* appears, which is not the same thing and the spec never
# separates them. A deadline you can see coming is calm; a deadline that
# appears together with the push is a scare.
DEADLINE_SUGGESTION_WINDOW_DAYS: Final = 7
DEADLINE_FLAG_WINDOW_DAYS: Final = 7
PREPARE_WINDOW_HOURS: Final = 48
FOLLOW_UP_EXPIRY_DAYS: Final = 14
LET_IT_GO_AFTER_DORMANT_DAYS: Final = 7


# ── Silence, in two tiers ───────────────────────────────────────────────────
#
# The first tier is the spec's: past `stale_after_days` — or twice your own p90
# for that stage, once there is enough history to know it — an application is
# `dormant`. That is a reversible judgement, and it is right: a recruiter who
# comes back after three weeks is common.
#
# The second tier does not exist in the spec, and it should. A dormant
# application still reads as something that might yet happen, so a pipeline
# accumulates processes everyone involved has forgotten about, and the daily
# view keeps offering to chase them. Past this threshold the honest reading is
# that you were passed over without being told — which is what a ghost rate is
# measuring in the first place.
#
# The number is not a guess. Across a real twelve-month mailbox the longest
# silence ever followed by a reply was **20 days**; every other gap was under
# two weeks. Ninety days is four and a half times the longest observed revival,
# which leaves generous room for the case the data has not seen while still
# clearing the pipeline within a quarter.
#
# It stays a setting because that evidence is one person's mailbox, and an
# industry that moves slower would need a longer number.
PRESUMED_CLOSED_DAYS: Final = 90
# Never presume closure while the ball is in the user's court.
PRESUMED_CLOSED_SKIP_STAGES: Final = frozenset({"take_home", "offer", "negotiating"})


# ── Connector (Spec §06) ────────────────────────────────────────────────────
WATCH_RENEW_EVERY_HOURS: Final = 24
WATCH_RENEW_FAILURES_BEFORE_POLLING: Final = 3
POLL_INTERVAL_SECONDS: Final = 300
BACKFILL_BATCH: Final = 250
BACKFILL_CONCURRENCY: Final = 2
# Gmail forgets history ids older than this; a 404 means full re-list.
HISTORY_HORIZON_DAYS: Final = 7
RELIST_DAYS: Final = 30
BACKOFF_MIN_SECONDS: Final = 1
BACKOFF_MAX_SECONDS: Final = 64
BACKOFF_ATTEMPTS: Final = 8
# decisions.md C8 — "everything" needs a bound or backfill never ends.
MAX_BACKFILL_MONTHS: Final = 60


# ── Queue (Spec §05) ────────────────────────────────────────────────────────
VISIBILITY_TIMEOUT_SECONDS: Final = 60
MAX_ATTEMPTS: Final = 5
# decisions.md C9 — nothing in the spec drained the park; this does.
PARK_RETRY_EVERY_MINUTES: Final = 15
PARK_MAX_ATTEMPTS: Final = 6


# ── Observability (Spec §16): the one question that matters ─────────────────
FRESHNESS_WARN_AFTER_HOURS: Final = 2
FRESHNESS_ALERT_AFTER_HOURS: Final = 12
OLDEST_UNPROCESSED_ALERT_MINUTES: Final = 30


# ── Extraction pre-processing (Spec §08) ────────────────────────────────────
MAX_TEXT_CHARS: Final = 6_000
# A model's self-reported certainty is not calibrated.
MODEL_CONFIDENCE_DISCOUNT: Final = 0.9
MODEL_MAX_TOKENS: Final = 1_500

# Review excerpts are display-only and never longer than this (Spec §04).
REVIEW_EXCERPT_MAX_CHARS: Final = 280
