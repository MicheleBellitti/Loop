"""Role-title embeddings.

The spec picks bge-small-en-v1.5, 384 dims, in-process. What is implemented here
is the stand-in it shipped with: deterministic feature hashing over word
unigrams, bigrams and character 4-grams into the same 384 dimensions, so the
schema, the vector index and the cosine thresholds are exercised exactly as
specified with no model download and no network.

Ported bit-for-bit rather than reimplemented, and that is the point. The
resolver's thresholds — attach at 0.72, attach-among-many at 0.82, merge at 0.93
— were tuned against *this* function's output. A different embedder is a
different geometry, and carrying the numbers across would be a silent
regression. P4 replaces it with sentence-transformers and re-tunes them against
the corpus; until then the two implementations must agree on every digit or the
phase-2 differential means nothing.
"""

import math
import os
from collections.abc import Mapping
from hashlib import sha1
from itertools import pairwise
from typing import Protocol

EMBEDDING_DIMS = 384

# Character 4-grams are evidence, but weaker than a word: they exist to make
# "backend engineer" and "back-end engineering" neighbours, not to outvote the
# words themselves.
_NGRAM_WEIGHT = 0.4
_NGRAM_PREFIX = "~"
_NGRAM_SIZE = 4


class Embedder(Protocol):
    @property
    def name(self) -> str: ...

    def embed(self, text: str) -> list[float]: ...


def _hash_to_index(token: str, salt: str) -> int:
    digest = sha1(f"{salt}:{token}".encode()).digest()
    return ((digest[0] << 16) | (digest[1] << 8) | digest[2]) % EMBEDDING_DIMS


def _hash_sign(token: str, salt: str) -> int:
    """The ±1 hashing trick, so collisions cancel instead of accumulating."""
    digest = sha1(f"sign:{salt}:{token}".encode()).digest()
    return 1 if digest[0] % 2 == 0 else -1


def _tokens(text: str) -> list[str]:
    words = [w for w in _split(text.lower()) if w]
    out = list(words)
    out.extend(f"{a} {b}" for a, b in pairwise(words))
    flat = " ".join(words)
    out.extend(
        f"{_NGRAM_PREFIX}{flat[i : i + _NGRAM_SIZE]}"
        for i in range(len(flat) - _NGRAM_SIZE + 1)
    )
    return out


_KEPT = set("abcdefghijklmnopqrstuvwxyz0123456789+#")


def _split(text: str) -> list[str]:
    """Split on everything that is not a letter, digit, `+` or `#`.

    Written out rather than as a regex because the character class has to match
    the TypeScript's `[^a-z0-9+#]+` exactly, and a Unicode-aware `\\w` in Python
    would quietly keep accented letters the reference dropped.
    """
    words: list[str] = []
    current: list[str] = []
    for char in text:
        if char in _KEPT:
            current.append(char)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


class LexicalEmbedder:
    name = "lexical-hash-384"

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMS
        counts: dict[str, int] = {}
        for token in _tokens(text):
            counts[token] = counts.get(token, 0) + 1

        for token, count in counts.items():
            # Sub-linear term frequency: a word repeated five times is not five
            # times the evidence, and role titles repeat words often
            # ("engineer, engineering").
            weight = (1 + math.log(count)) * (
                _NGRAM_WEIGHT if token.startswith(_NGRAM_PREFIX) else 1
            )
            vector[_hash_to_index(token, "idx")] += weight * _hash_sign(token, "idx")

        norm = math.hypot(*vector) or 1.0
        return [x / norm for x in vector]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def to_vector(values: list[float]) -> str:
    """A Postgres `vector` literal."""
    return f"[{','.join(repr(v) for v in values)}]"


def parse_vector(raw: str | None) -> list[float]:
    if not raw:
        return []
    inner = raw.strip().removeprefix("[").removesuffix("]")
    return [float(part) for part in inner.split(",")] if inner else []


class SentenceTransformerEmbedder:
    """bge-small-en-v1.5 in process, which is what the spec asked for.

    Loaded lazily and imported inside the method, so a deployment that never
    sets `EMBEDDING_MODEL` never pays for torch. The reference's escape hatch was
    an `OnnxEmbedder` that raised on every call — a placeholder for a tokenizer
    nobody bound — so this is the first version of it that returns a vector.

    **Switching is not free, and it is not a configuration change.** The
    resolver's thresholds were tuned against `LexicalEmbedder`'s geometry:
    attach at 0.72, attach-among-many at 0.82, merge at 0.93. A real encoder puts
    unrelated titles at cosines the lexical hash never produced, and a merge
    threshold that is too low rewrites two applications' history into one
    silently. Re-tune against the corpus with the harness first (§3.3); that is
    P4, and this class is what P4 turns on.
    """

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        self.name = model
        self._model = model
        self._encoder: object | None = None

    def embed(self, text: str) -> list[float]:
        encoder = self._loaded()
        vector = encoder.encode(text, normalize_embeddings=True)  # type: ignore[attr-defined]
        values = [float(x) for x in vector]
        if len(values) != EMBEDDING_DIMS:
            raise ValueError(
                f"{self._model} produced {len(values)} dimensions; the schema's "
                f"vector column is {EMBEDDING_DIMS}. Changing it is a migration, "
                "not a setting."
            )
        return values

    def _loaded(self) -> object:
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as error:  # pragma: no cover - depends on the extra
                raise RuntimeError(
                    "EMBEDDING_MODEL is set but sentence-transformers is not "
                    "installed. Install the `ml` extra, or unset the variable to "
                    "use the lexical embedder."
                ) from error
            self._encoder = SentenceTransformer(self._model)
        return self._encoder


def create_embedder(env: Mapping[str, str] | None = None) -> Embedder:
    """Lexical unless told otherwise, and told otherwise is a deliberate act."""
    source = os.environ if env is None else env
    model = (source.get("EMBEDDING_MODEL") or "").strip()
    return SentenceTransformerEmbedder(model) if model else LexicalEmbedder()
