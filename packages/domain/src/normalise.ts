/**
 * Normalisation used by the resolver, kept pure and here so the golden corpus
 * can test it without a database. Engineering Spec §09 specifies the behaviour
 * in one line each; this is that line made explicit.
 */

/** Legal forms, stripped so "Nexi S.p.A." and "Nexi" are one company. */
const LEGAL_SUFFIXES = [
  's.r.l.s.', 's.r.l.', 'srls', 'srl', 's.p.a.', 'spa', 's.a.s.', 'sas', 's.n.c.', 'snc',
  's.a.p.a.', 'sapa', 'gmbh', 'mbh', 'ag', 'kg', 'ohg', 'ug',
  'inc.', 'inc', 'llc', 'l.l.c.', 'ltd.', 'ltd', 'limited', 'plc', 'corp.', 'corp',
  'corporation', 'company', 'co.', 'b.v.', 'bv', 'n.v.', 'nv', 's.a.', 'sa', 's.à.r.l.',
  'sarl', 'oy', 'oyj', 'ab', 'a/s', 'as', 'aps', 'kft', 'zrt', 'd.o.o.', 'doo', 'pte',
  'pty', 'sp. z o.o.', 'sp z oo',
];

/**
 * Deliberately excluded from the list above: "group" and "holding". They are
 * frequently part of the name a company actually trades under, and stripping
 * them would merge two different legal entities in the same family.
 */

const SENIORITY_WORDS: Record<string, string> = {
  jr: 'junior', 'jr.': 'junior', junior: 'junior', grad: 'junior', graduate: 'junior',
  entry: 'junior', intern: 'intern', internship: 'intern', trainee: 'intern',
  mid: 'mid', middle: 'mid', 'mid-level': 'mid',
  sr: 'senior', 'sr.': 'senior', senior: 'senior', snr: 'senior',
  staff: 'staff', principal: 'principal', lead: 'lead', head: 'head',
  director: 'director', vp: 'vp', chief: 'chief',
  i: 'mid', ii: 'mid', iii: 'senior', iv: 'senior',
};

/** Abbreviations the spec names, plus the ones that appear in the same mail. */
const EXPANSIONS: Record<string, string> = {
  sr: 'senior', jr: 'junior',
  eng: 'engineer', engr: 'engineer', engineering: 'engineer',
  dev: 'developer', devel: 'developer',
  be: 'backend', 'back-end': 'backend', 'back end': 'backend',
  fe: 'frontend', 'front-end': 'frontend', 'front end': 'frontend',
  fs: 'fullstack', 'full-stack': 'fullstack', 'full stack': 'fullstack',
  swe: 'software engineer', sde: 'software engineer',
  sre: 'site reliability engineer',
  ml: 'machine learning', ai: 'artificial intelligence',
  qa: 'quality assurance', pm: 'product manager', po: 'product owner',
  ux: 'user experience', ui: 'user interface',
  db: 'database', k8s: 'kubernetes', infra: 'infrastructure', ops: 'operations',
};

const CONTRACT_TERMS = [
  'full time', 'full-time', 'fulltime', 'part time', 'part-time', 'parttime',
  'permanent', 'temporary', 'contract', 'contractor', 'freelance', 'fixed term',
  'fixed-term', 'internship', 'apprenticeship', 'tempo indeterminato',
  'tempo determinato', 'stage', 'tirocinio', 'unbefristet', 'festanstellung',
  'm/f/d', 'f/m/d', 'm/w/d', 'w/m/d', 'm/f/x', 'd/f/m', 'h/f', 'm/f',
];

const WORK_MODES: Array<[RegExp, 'onsite' | 'hybrid' | 'remote']> = [
  [/\b(fully\s+)?remote\b|\bda\s+remoto\b|\bsmart\s*working\b|\bwfh\b/i, 'remote'],
  [/\bhybrid\b|\bibrido\b/i, 'hybrid'],
  [/\bon[-\s]?site\b|\bin[-\s]?office\b|\bin\s+sede\b/i, 'onsite'],
];

/** Small gazetteer — enough to recognise a trailing location, not a geocoder. */
const PLACES = new Set([
  'milan', 'milano', 'rome', 'roma', 'turin', 'torino', 'naples', 'napoli', 'bologna',
  'florence', 'firenze', 'venice', 'venezia', 'genoa', 'genova', 'bari', 'palermo',
  'trento', 'trieste', 'padova', 'padua', 'verona', 'brescia', 'bergamo', 'biassono',
  'berlin', 'munich', 'münchen', 'hamburg', 'frankfurt', 'cologne', 'köln', 'stuttgart',
  'london', 'manchester', 'dublin', 'edinburgh', 'paris', 'lyon', 'toulouse',
  'madrid', 'barcelona', 'lisbon', 'lisboa', 'porto', 'amsterdam', 'rotterdam',
  'brussels', 'zurich', 'zürich', 'geneva', 'vienna', 'wien', 'prague', 'warsaw',
  'stockholm', 'copenhagen', 'oslo', 'helsinki', 'tallinn', 'vilnius', 'riga',
  'bucharest', 'sofia', 'budapest', 'athens', 'istanbul', 'krakow', 'kraków',
  'italy', 'italia', 'germany', 'deutschland', 'france', 'spain', 'españa', 'portugal',
  'netherlands', 'belgium', 'switzerland', 'austria', 'poland', 'sweden', 'denmark',
  'norway', 'finland', 'ireland', 'uk', 'united kingdom', 'europe', 'eu', 'emea',
  'remote', 'hybrid', 'onsite', 'on-site', 'anywhere',
]);

const SEPARATORS = /\s+[-–—|·]\s+|\s*[/,]\s*/;

function stripAccentsAndPunct(s: string): string {
  return s
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^\p{L}\p{N}\s.&+-]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Casefold, strip the legal suffix, collapse whitespace. This is the key
 * `by_alias` looks up, so it must be stable: two spellings of one company have
 * to land on the same string or the resolver creates a duplicate application.
 */
export function normaliseCompany(raw: string): string {
  let s = stripAccentsAndPunct(raw.toLowerCase());
  // Suffixes can stack ("Foo Italia S.r.l."), so strip repeatedly.
  let changed = true;
  while (changed) {
    changed = false;
    for (const suf of LEGAL_SUFFIXES) {
      const re = new RegExp(`(^|\\s)${suf.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`, 'i');
      if (re.test(s)) {
        s = s.replace(re, '').trim();
        changed = true;
      }
    }
  }
  return s.replace(/[\s.&+-]+$/g, '').replace(/\s+/g, ' ').trim();
}

/**
 * The key two spellings of one employer have to agree on.
 *
 * `normaliseCompany` folds case, accents and legal suffixes, which is enough
 * until the same company arrives by two routes: an ATS puts "ION Group" in the
 * From display name, while the company's own mail resolves through its domain
 * to "iongroup". Those differ by one space and became two companies, two
 * pipelines and two sets of statistics.
 *
 * The key drops everything that is not a letter or a digit, so spacing,
 * hyphenation and punctuation stop mattering. It is only ever a lookup key —
 * the human-readable `canonical_name` keeps its spaces.
 */
export function companyKey(raw: string): string {
  return normaliseCompany(raw).replace(/[^\p{L}\p{N}]+/gu, '');
}

export interface NormalisedRole {
  /** The comparison key — what gets embedded and compared by cosine. */
  role: string;
  /** Pulled out of the title into its own field, as the spec requires. */
  seniority: string | null;
  location: string | null;
  workMode: 'onsite' | 'hybrid' | 'remote' | null;
}

function looksLikePlace(segment: string): boolean {
  const s = segment.trim().toLowerCase();
  if (!s) return false;
  if (PLACES.has(s)) return true;
  // "Milan · hybrid", "Berlin (remote)" — any word of a short segment matching.
  const words = s.split(/\s+/);
  return words.length <= 3 && words.some((w) => PLACES.has(w));
}

function isContractTerm(segment: string): boolean {
  const s = segment.trim().toLowerCase();
  return CONTRACT_TERMS.some((t) => s === t || s.includes(t));
}

/**
 * "Senior Backend Engineer (m/f/d) - Milan, full time"
 *   → { role: "backend engineer", seniority: "senior",
 *       location: "Milan", workMode: null }
 */
export function normaliseRole(raw: string): NormalisedRole {
  let workMode: 'onsite' | 'hybrid' | 'remote' | null = null;
  for (const [re, mode] of WORK_MODES) {
    if (re.test(raw)) {
      workMode = mode;
      break;
    }
  }

  // Bracketed groups are almost always contract or diversity notation.
  const bracketed: string[] = [];
  let s = raw.replace(/[([{]([^)\]}]*)[)\]}]/g, (_m, inner: string) => {
    bracketed.push(inner);
    return ' ';
  });

  let location: string | null = null;
  for (const b of bracketed) {
    if (looksLikePlace(b)) location = b.trim();
  }

  // Walk the separated segments and stop at the first one that is a place or a
  // contract term — everything from there on is trailing metadata, not a title.
  const segments = s.split(SEPARATORS).map((x) => x.trim()).filter(Boolean);
  const kept: string[] = [];
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i]!;
    if (i > 0 && (looksLikePlace(seg) || isContractTerm(seg))) {
      if (looksLikePlace(seg) && !location) location = seg;
      break;
    }
    kept.push(seg);
  }
  s = kept.join(' ');

  for (const t of CONTRACT_TERMS) {
    s = s.replace(new RegExp(`\\b${t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'gi'), ' ');
  }

  s = stripAccentsAndPunct(s.toLowerCase());

  // Tokenise, lift seniority out, expand abbreviations.
  const tokens = s.split(/\s+/).filter(Boolean);
  let seniority: string | null = null;
  const out: string[] = [];
  for (const tok of tokens) {
    const bare = tok.replace(/[.]+$/, '');
    const sen = SENIORITY_WORDS[bare];
    if (sen && !seniority) {
      seniority = sen;
      continue;
    }
    if (sen) continue;
    out.push(EXPANSIONS[bare] ?? bare);
  }

  const role = out.join(' ').replace(/\s+/g, ' ').trim();
  return { role, seniority, location, workMode };
}

/** `user@mail.company.com` → `mail.company.com`. Lowercased, no angle brackets. */
export function domainOfAddress(address: string): string | null {
  const m = /<?([^<>\s]+)@([A-Za-z0-9.-]+)>?/.exec(address.trim());
  return m ? m[2]!.toLowerCase().replace(/\.$/, '') : null;
}

/** `mail.greenhouse.io` → `greenhouse.io` for a known vendor suffix match. */
export function matchesDomainSuffix(domain: string, candidate: string): boolean {
  const d = domain.toLowerCase();
  const c = candidate.toLowerCase();
  return d === c || d.endsWith(`.${c}`);
}
