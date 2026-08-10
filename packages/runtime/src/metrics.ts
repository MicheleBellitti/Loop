/**
 * The counters §16 asks to expose, in Prometheus text format on an internal
 * /metrics endpoint. No client library: a counter map and a serialiser are
 * eighty lines, and the alternative is a dependency that pulls in a registry, a
 * default-metrics collector and a cluster aggregator for a box with one user.
 */

type Labels = Record<string, string | number>;

interface Series {
  help: string;
  type: 'counter' | 'gauge' | 'histogram';
  values: Map<string, number>;
  buckets?: number[];
  sums?: Map<string, number>;
  counts?: Map<string, number>;
}

const registry = new Map<string, Series>();

function key(labels: Labels = {}): string {
  const entries = Object.entries(labels).sort(([a], [b]) => a.localeCompare(b));
  return entries.map(([k, v]) => `${k}="${String(v).replace(/"/g, '\\"')}"`).join(',');
}

function series(name: string, help: string, type: Series['type'], buckets?: number[]): Series {
  let s = registry.get(name);
  if (!s) {
    s = { help, type, values: new Map(), buckets, sums: new Map(), counts: new Map() };
    registry.set(name, s);
  }
  return s;
}

export function counter(name: string, help: string) {
  const s = series(name, help, 'counter');
  return {
    inc(labels: Labels = {}, by = 1): void {
      const k = key(labels);
      s.values.set(k, (s.values.get(k) ?? 0) + by);
    },
  };
}

export function gauge(name: string, help: string) {
  const s = series(name, help, 'gauge');
  return {
    set(value: number, labels: Labels = {}): void {
      s.values.set(key(labels), value);
    },
  };
}

export function histogram(name: string, help: string, buckets: number[]) {
  const s = series(name, help, 'histogram', buckets);
  return {
    observe(value: number, labels: Labels = {}): void {
      const base = key(labels);
      for (const b of buckets) {
        if (value <= b) {
          const k = `${base}${base ? ',' : ''}le="${b}"`;
          s.values.set(k, (s.values.get(k) ?? 0) + 1);
        }
      }
      const kInf = `${base}${base ? ',' : ''}le="+Inf"`;
      s.values.set(kInf, (s.values.get(kInf) ?? 0) + 1);
      s.sums!.set(base, (s.sums!.get(base) ?? 0) + value);
      s.counts!.set(base, (s.counts!.get(base) ?? 0) + 1);
    },
  };
}

export function renderMetrics(): string {
  const out: string[] = [];
  for (const [name, s] of registry) {
    out.push(`# HELP ${name} ${s.help}`);
    out.push(`# TYPE ${name} ${s.type}`);
    for (const [labels, value] of s.values) {
      const suffix = s.type === 'histogram' ? '_bucket' : '';
      out.push(`${name}${suffix}${labels ? `{${labels}}` : ''} ${value}`);
    }
    if (s.type === 'histogram') {
      for (const [labels, sum] of s.sums!) out.push(`${name}_sum${labels ? `{${labels}}` : ''} ${sum}`);
      for (const [labels, n] of s.counts!) out.push(`${name}_count${labels ? `{${labels}}` : ''} ${n}`);
    }
  }
  return `${out.join('\n')}\n`;
}

// The catalogue from §16, declared once so a typo in a label name is a compile
// error somewhere rather than a metric that silently never appears.
export const M = {
  messagesRead: counter('messages_read_total', 'Messages fetched from a provider'),
  messagesDropped: counter('messages_dropped_total', 'Messages the classifier dropped'),
  extraction: counter('extraction_total', 'Extraction attempts by rung and outcome'),
  modelLatency: histogram('model_latency_seconds', 'Rung 3 call latency', [0.5, 1, 2, 5, 10, 20, 30, 60]),
  modelFailures: counter('model_failures_total', 'Rung 3 failures by kind'),
  resolverDecisions: counter('resolver_decisions_total', 'Resolver outcomes'),
  reviewItemsOpen: gauge('review_items_open', 'Open review items'),
  denylistViolations: counter('denylist_violations_total', 'Article 9 fields dropped from model output'),
  eventsAppended: counter('events_appended_total', 'Events appended by type'),
  notificationsSent: counter('notifications_sent_total', 'Push notifications by rule'),
  mailboxFreshnessSeconds: gauge('mailbox_freshness_seconds', 'Seconds since a mailbox last read successfully'),
  queueDepth: gauge('queue_depth', 'Messages waiting per queue'),
  deadLetters: gauge('dead_letters', 'Messages in a dead-letter queue'),
};
