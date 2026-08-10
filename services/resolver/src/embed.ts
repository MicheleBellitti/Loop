import { createHash } from 'node:crypto';

/**
 * Role-title embeddings.
 *
 * §02 picks bge-small-en-v1.5 via ONNX, 384 dims, in-process, "~30 MB,
 * CPU-fast, no service to run; role titles are short strings". That is
 * implemented below as `OnnxEmbedder` and enabled by pointing
 * `EMBEDDING_MODEL_PATH` at the model.
 *
 * The default is `LexicalEmbedder`: deterministic feature hashing over word
 * unigrams, bigrams and character 4-grams, into the same 384 dimensions, so the
 * schema, the HNSW index and the cosine thresholds are all exercised exactly as
 * specified — with no model download, no native addon, and no network. For
 * titles that have already been through `normaliseRole` (seniority lifted out,
 * abbreviations expanded, location stripped) the two agree closely enough that
 * the corpus is the arbiter, which is what §09 asks for anyway: thresholds are
 * "tuned only against the golden corpus, and changed only with the
 * precision/recall table in the PR".
 *
 * Switching embedder therefore means re-running `npm run test:corpus` and
 * re-reading that table. decisions.md B16.
 */

export const EMBEDDING_DIMS = 384;

export interface Embedder {
  readonly name: string;
  embed(text: string): Promise<number[]>;
}

function hashToIndex(token: string, salt: string): number {
  const digest = createHash('sha1').update(`${salt}:${token}`).digest();
  return ((digest[0]! << 16) | (digest[1]! << 8) | digest[2]!) % EMBEDDING_DIMS;
}

/** ±1 hashing trick, so collisions cancel instead of accumulating. */
function hashSign(token: string, salt: string): number {
  return createHash('sha1').update(`sign:${salt}:${token}`).digest()[0]! % 2 === 0 ? 1 : -1;
}

function tokens(text: string): string[] {
  const words = text.toLowerCase().split(/[^a-z0-9+#]+/).filter(Boolean);
  const out: string[] = [...words];
  for (let i = 0; i + 1 < words.length; i++) out.push(`${words[i]} ${words[i + 1]}`);
  const flat = words.join(' ');
  for (let i = 0; i + 4 <= flat.length; i++) out.push(`~${flat.slice(i, i + 4)}`);
  return out;
}

export class LexicalEmbedder implements Embedder {
  readonly name = 'lexical-hash-384';

  async embed(text: string): Promise<number[]> {
    const v = new Array<number>(EMBEDDING_DIMS).fill(0);
    const seen = new Map<string, number>();
    for (const t of tokens(text)) seen.set(t, (seen.get(t) ?? 0) + 1);

    for (const [token, count] of seen) {
      // Sub-linear term frequency: a word repeated five times is not five times
      // as much evidence, and role titles repeat words often ("engineer,
      // engineering").
      const weight = (1 + Math.log(count)) * (token.startsWith('~') ? 0.4 : 1);
      const i = hashToIndex(token, 'idx');
      v[i]! += weight * hashSign(token, 'idx');
    }

    let norm = Math.hypot(...v);
    if (norm === 0) norm = 1;
    return v.map((x) => x / norm);
  }
}

/**
 * The spec's choice, loaded lazily so the dependency is only paid for when it
 * is configured. Requires `onnxruntime-node` and a local copy of the model.
 */
export class OnnxEmbedder implements Embedder {
  readonly name = 'bge-small-en-v1.5';
  private session: unknown = null;

  constructor(private readonly modelPath: string) {}

  async embed(text: string): Promise<number[]> {
    if (!this.session) {
      // Imported through a computed specifier so TypeScript does not require
      // the package to be installed: it is optional, and the lexical embedder
      // is the default.
      const specifier = 'onnxruntime-node';
      const ort = (await import(/* @vite-ignore */ specifier).catch(() => null)) as {
        InferenceSession: { create(p: string): Promise<unknown> };
      } | null;
      if (!ort) {
        throw new Error(
          'EMBEDDING_MODEL_PATH is set but onnxruntime-node is not installed. ' +
            'Run `npm i onnxruntime-node -w @loop/resolver`, or unset the variable to use the lexical embedder.',
        );
      }
      this.session = await ort.InferenceSession.create(this.modelPath);
    }
    throw new Error(
      'The ONNX embedder needs a tokenizer bound to the model checkpoint; ' +
        'see docs/embeddings.md before enabling it.',
    );
  }
}

export function createEmbedder(env = process.env): Embedder {
  const path = env.EMBEDDING_MODEL_PATH?.trim();
  return path ? new OnnxEmbedder(path) : new LexicalEmbedder();
}

export function cosine(a: readonly number[], b: readonly number[]): number {
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i]! * b[i]!;
    na += a[i]! * a[i]!;
    nb += b[i]! * b[i]!;
  }
  if (na === 0 || nb === 0) return 0;
  return dot / Math.sqrt(na * nb);
}

/** Postgres `vector` literal. */
export function toVector(v: readonly number[]): string {
  return `[${v.join(',')}]`;
}
