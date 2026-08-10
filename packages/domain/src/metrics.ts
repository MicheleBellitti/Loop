import { GATES } from './thresholds.js';

/**
 * Metric envelopes and the display gates.
 *
 * "Each response carries its own numerator, denominator and exclusion count —
 * the UI is required to show the denominator, so the API is required to send
 * it." Shipping a ratio without its note is called out in the handoff as a bug,
 * so the note is part of the value, not part of the template.
 */

export interface Metric {
  /** Null whenever the gate is unmet — there is no honest number to show. */
  value: number | null;
  numerator: number;
  denominator: number;
  /** Applications deliberately left out, e.g. too recent to have converted. */
  excluded: number;
  gate_met: boolean;
  /** "11 of 68 · 5 too recent to count" or "Unlocks at 8 closed applications". */
  note: string;
  /** Between the two gates the figure ships, flagged. */
  small_sample: boolean;
}

export interface RatioInput {
  numerator: number;
  denominator: number;
  excluded?: number;
  /** How many closed applications back this ratio. */
  closed: number;
  exclusionReason?: string;
}

/**
 * A ratio and the sentence that makes it honest.
 *
 * Below the gate the value is null and the note names the threshold, which is
 * what turns an empty chart into a progress bar rather than a disappointment.
 */
export function ratio(input: RatioInput): Metric {
  const excluded = input.excluded ?? 0;
  const gate_met = input.closed >= GATES.RATIOS_MIN_CLOSED;
  const small_sample = gate_met && input.closed <= GATES.SMALL_SAMPLE_MAX;

  if (!gate_met) {
    return {
      value: null,
      numerator: input.numerator,
      denominator: input.denominator,
      excluded,
      gate_met: false,
      small_sample: false,
      note: `${input.closed} closed · unlocks at ${GATES.RATIOS_MIN_CLOSED} closed applications`,
    };
  }

  const parts = [`${input.numerator} of ${input.denominator}`];
  if (excluded > 0) parts.push(`${excluded} ${input.exclusionReason ?? 'excluded'}`);
  if (small_sample) parts.push('small sample');

  return {
    value: input.denominator === 0 ? null : input.numerator / input.denominator,
    numerator: input.numerator,
    denominator: input.denominator,
    excluded,
    gate_met: true,
    small_sample,
    note: parts.join(' · '),
  };
}

/** Median dwell per stage. Gated on observed transitions, not on applications. */
export function dwellMetric(p50: number | null, transitions: number): Metric {
  const gate_met = transitions >= GATES.TIME_IN_STAGE_MIN_TRANSITIONS;
  return {
    value: gate_met ? p50 : null,
    numerator: p50 ?? 0,
    denominator: transitions,
    excluded: 0,
    gate_met,
    small_sample: false,
    note: gate_met
      ? `${transitions} transitions`
      : `${transitions} of ${GATES.TIME_IN_STAGE_MIN_TRANSITIONS} stage changes needed`,
  };
}

/** A channel row is only shown once it has enough first-touch applications. */
export function channelGate(applications: number): { gate_met: boolean; note: string } {
  const gate_met = applications >= GATES.CHANNEL_MIN_APPLICATIONS;
  return {
    gate_met,
    note: gate_met ? '' : `${applications} of ${GATES.CHANNEL_MIN_APPLICATIONS} needed`,
  };
}

/** Seasonal shape is withheld rather than shown noisy. */
export function seasonalGate(quarters: number): { gate_met: boolean; note: string } {
  const gate_met = quarters >= GATES.SEASONAL_MIN_QUARTERS;
  return {
    gate_met,
    note: gate_met
      ? ''
      : `Seasonal averages need ${GATES.SEASONAL_MIN_QUARTERS - quarters} more quarter${
          GATES.SEASONAL_MIN_QUARTERS - quarters === 1 ? '' : 's'
        } of history before they mean anything, so they are hidden rather than shown noisy.`,
  };
}

/** "16.2%" — one decimal, because two implies a precision we do not have. */
export function formatPercent(value: number | null): string {
  if (value === null) return '—';
  return `${(value * 100).toFixed(1).replace(/\.0$/, '')}%`;
}

export function formatDays(value: number | null): string {
  if (value === null) return '—';
  const rounded = Math.round(value * 10) / 10;
  return `${rounded} day${rounded === 1 ? '' : 's'}`;
}
