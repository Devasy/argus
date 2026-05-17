"""Identifier-aware lexical retrieval over the learning corpus.

Deliberately in Python rather than Postgres `tsvector`. Two reasons: the
`english` config neither splits `computeTotals` nor stems identifiers usefully,
and the corpus is small enough (hundreds of rows per repo) that scoring every
document costs less than a round trip. Scoring the whole corpus also removes
the candidate-pool ceiling that caps the vector arm -- there is no top-N
prefilter for a relevant learning to fall outside of.
"""
import math
import re
from collections import Counter

K1 = 1.2
B = 0.75
RRF_K = 60
MIN_TOKEN_LEN = 2

_WORD = re.compile(r"[A-Za-z0-9_]+")
_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def tokenize(text: str) -> list[str]:
    """Emit each identifier whole AND split into its parts, so a query saying
    `maxRetryCount` matches a learning written as `max_retry_count`."""
    out: list[str] = []
    for word in _WORD.findall(text or ""):
        whole = word.lower()
        if len(whole) >= MIN_TOKEN_LEN:
            out.append(whole)
        parts = [p.lower() for chunk in word.split("_") for p in _PART.findall(chunk)]
        if len(parts) > 1 or (parts and parts[0] != whole):
            out.extend(p for p in parts if len(p) >= MIN_TOKEN_LEN)
    return out


class Bm25Index:
    """Okapi BM25 over an in-memory {doc_id: text} corpus."""

    def __init__(self, docs: dict[str, str]):
        self._tf: dict[str, Counter] = {}
        self._len: dict[str, int] = {}
        df: Counter = Counter()
        for doc_id, text in docs.items():
            terms = tokenize(text)
            self._tf[doc_id] = Counter(terms)
            self._len[doc_id] = len(terms)
            df.update(set(terms))
        self._n = len(docs)
        self._df = df
        self._avgdl = (sum(self._len.values()) / self._n) if self._n else 0.0

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        if df == 0:
            return 0.0
        return math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))

    def rank(self, query: str, limit: int) -> list[tuple[str, float]]:
        terms = [t for t in dict.fromkeys(tokenize(query)) if self._df.get(t)]
        if not terms or not self._n:
            return []
        scored: list[tuple[str, float]] = []
        for doc_id, tf in self._tf.items():
            total = 0.0
            for term in terms:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                norm = freq + K1 * (1.0 - B + B * self._len[doc_id] / self._avgdl)
                total += self._idf(term) * freq * (K1 + 1.0) / norm
            if total > 0.0:
                scored.append((doc_id, total))
        scored.sort(key=lambda r: (-r[1], r[0]))
        return scored[:limit]


def rrf(ranked_lists: list[list[str]], k: int = RRF_K,
        weights: list[float] | None = None) -> dict[str, float]:
    """Reciprocal Rank Fusion. Ranks, not scores, because cosine distance and
    BM25 share no scale and blending them needs constants that are quietly
    wrong. An id absent from a list simply contributes nothing for it."""
    out: dict[str, float] = {}
    for i, ids in enumerate(ranked_lists):
        weight = weights[i] if weights else 1.0
        for rank, doc_id in enumerate(dict.fromkeys(ids)):
            out[doc_id] = out.get(doc_id, 0.0) + weight / (k + rank + 1)
    return out
