/**
 * Every tunable number in the system, in one place.
 *
 * Engineering Spec §09: these are "tuned only against the golden corpus, and
 * changed only with the precision/recall table in the PR". `npm run test:corpus`
 * prints that table.
 */

/** Resolver — entity resolution thresholds (Spec §09). */
export const RESOLVER = {
  /** One candidate application at this company: attach above this cosine. */
  ATTACH_SINGLE: 0.72,
  /** Several candidates: attach to the best only above this cosine. */
  ATTACH_MULTI: 0.82,
  /** …and only if it beats the runner-up by at least this much. */
  AMBIGUITY_MARGIN: 0.05,
  /** Cross-channel dedup: the same job found twice. */
  DEDUP_MERGE: 0.93,
  /** Below this confidence a signal never reaches the fold — it asks the human. */
  REVIEW_BELOW: 0.6,
  /** Two applications only merge if they were created within this window. */
  DEDUP_WINDOW_DAYS: 14,
  /** An automatic merge stays one tap from being undone for this long (D5). */
  MERGE_UNDO_DAYS: 14,
} as const;

/**
 * The fold ignores anything below this. It is the same number as REVIEW_BELOW
 * on purpose: a signal is either good enough to change your pipeline or good
 * enough to be a question, never both and never neither.
 */
export const FOLD_CONFIDENCE_FLOOR = RESOLVER.REVIEW_BELOW;

/** A human's confidence. Pins the field until another 1.0 event touches it. */
export const PINNED_CONFIDENCE = 1.0;

/**
 * Display gates (Spec §11). The client MUST honour these; below a gate it shows
 * the count and names the threshold, which turns an empty chart into a progress
 * bar rather than a disappointment.
 */
export const GATES = {
  /** Ratios need this many closed applications before a percentage is honest. */
  RATIOS_MIN_CLOSED: 8,
  /** Between the two, the figure ships with a small-sample warning. */
  SMALL_SAMPLE_MAX: 15,
  /** Median dwell needs this many observed transitions. */
  TIME_IN_STAGE_MIN_TRANSITIONS: 5,
  /** Seasonal shape needs two quarters before it means anything. */
  SEASONAL_MIN_QUARTERS: 2,
  /** A channel row needs this many first-touch applications. */
  CHANNEL_MIN_APPLICATIONS: 3,
} as const;

/**
 * Maturity exclusion. An application applied more recently than this and still
 * sitting in `sent` has not had time to convert, so counting it drags every
 * ratio down — the reason a naive funnel always looks like it is falling.
 */
export const RATIO_MATURITY_DAYS = 21;

/** Classifier scoring (Spec §07). Biased towards recall, deliberately. */
export const CLASSIFIER = {
  /** At or above: full ladder. */
  PASS: 3,
  /** 1..2: rungs 1-2 only, never the model. */
  CHEAP_ONLY: 1,
} as const;

/** Notification budget (Spec §12). */
export const NOTIFICATIONS = {
  MAX_OPEN_SUGGESTIONS: 3,
  MAX_PUSH_PER_DAY: 1,
  /** decisions.md C3 — the spec caps the daily push but never names the slot. */
  DAILY_SLOT: '18:00',
  QUIET_FROM: '21:00',
  QUIET_TO: '08:00',
  /** The only alert allowed past the cap, and the §19 quiet-hours exception. */
  DEADLINE_EXEMPT_FROM_CAP: true,
  DEADLINE_BREAKS_QUIET_HOURS: true,
  /**
   * When a push fires. §12 gives exactly these two moments.
   */
  DEADLINE_WARN_HOURS: [72, 12] as const,
  /**
   * When the *card* appears, which is not the same thing and the spec never
   * separates them. The prototype shows a take-home suggestion at "in 3 days"
   * and a row flag reading "Due Sunday 23:59" well before any push is due — a
   * deadline you can see coming is calm, a deadline that appears with the push
   * is a scare. Seven days for the surface, 72 h and 12 h for the interruption.
   */
  DEADLINE_SUGGESTION_WINDOW_DAYS: 7,
  DEADLINE_FLAG_WINDOW_DAYS: 7,
  PREPARE_WINDOW_HOURS: 48,
  FOLLOW_UP_EXPIRY_DAYS: 14,
  LET_IT_GO_AFTER_DORMANT_DAYS: 7,
} as const;

/** Connector posture (Spec §06). */
export const CONNECTOR = {
  WATCH_RENEW_EVERY_HOURS: 24,
  WATCH_RENEW_FAILURES_BEFORE_POLLING: 3,
  POLL_INTERVAL_MS: 5 * 60_000,
  BACKFILL_BATCH: 250,
  BACKFILL_CONCURRENCY: 2,
  /** Gmail forgets history ids older than this; a 404 means full re-list. */
  HISTORY_HORIZON_DAYS: 7,
  RELIST_DAYS: 30,
  BACKOFF_MIN_MS: 1_000,
  BACKOFF_MAX_MS: 64_000,
  BACKOFF_ATTEMPTS: 8,
  /** decisions.md C8 — "everything" needs a bound or backfill never ends. */
  MAX_BACKFILL_MONTHS: 60,
} as const;

/** Queue posture (Spec §05). */
export const QUEUE = {
  VISIBILITY_TIMEOUT_S: 60,
  MAX_ATTEMPTS: 5,
  /** decisions.md C9 — nothing in the spec drained the park; this does. */
  PARK_RETRY_EVERY_MIN: 15,
  PARK_MAX_ATTEMPTS: 6,
} as const;

/** Observability (Spec §16): the one question that matters. */
export const FRESHNESS = {
  WARN_AFTER_HOURS: 2,
  ALERT_AFTER_HOURS: 12,
  OLDEST_UNPROCESSED_ALERT_MIN: 30,
} as const;

/** Extraction pre-processing (Spec §08). */
export const EXTRACTOR = {
  MAX_TEXT_CHARS: 6_000,
  /** A model's self-reported certainty is not calibrated. */
  MODEL_CONFIDENCE_DISCOUNT: 0.9,
  MODEL_MAX_TOKENS: 1_500,
} as const;

/** Review excerpts are display-only and never longer than this (Spec §04). */
export const REVIEW_EXCERPT_MAX_CHARS = 280;
