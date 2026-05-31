"""Score a retrieval strategy offline against the manual gold standard.

Takes the frozen corpus from `retrieval_snapshot`, rebuilds the query for each
gold MR under a named strategy, ranks the corpus, and feeds the ranked ids to
`retrieval_eval.score_ranking`. No review run, no database, no LLM -- only the
embedding endpoint, and only for queries it has not embedded before.

    python -m argus.benchmark.retrieval_probe --snapshot snap.json \\
        --strategies baseline,hybrid --known-misses

`baseline` reproduces the shipped vector-only path. If it does not reproduce
the recorded 11/44, the probe is wrong and no other number here means anything.
"""
import argparse
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from argus.benchmark.retrieval_eval import MRResult, load_gold, report, score_ranking
from argus.domain.models import Learning
from argus.knowledge.learnings import (HYBRID_PATTERN_BONUS,
                                          HYBRID_STRENGTH_WEIGHT,
                                          hybrid_base_scores, rank_learnings)
from argus.knowledge.lexical import Bm25Index, RRF_K
from argus.knowledge.queries import (identifier_query, learnings_query_text,
                                         scout_query)

logger = logging.getLogger("argus.retrieval_probe")

RRF_UNIT = 1.0 / (RRF_K + 1)

# Measured against the live endpoint: it 500s somewhere between 12k and 16k
# chars, well below embeddings.MAX_EMBED_CHARS. The vector arm therefore cannot
# see a large diff at all, whatever budget the query builder is given.
EMBED_INPUT_CEILING = 12_000
EMBED_MIN_CHARS = 500

# Named in the handoff: relevant, in the candidate pool, never retrieved.
KNOWN_MISSES = {
    12: ["e1d79ef3"],
    32: ["204b1fb3", "4da0e0c1", "cb7fc74d"],
    49: ["23cba962", "596b33cd", "2af903a8"],
    60: ["cc964a5a"],
}


@dataclass
class MRSnap:
    project_path: str
    mr_iid: int
    repo_id: str
    title: str
    changed_paths: list[str]
    added_lines: list[str]
    scout: dict | None

    @property
    def key(self) -> tuple[str, int]:
        return (self.project_path, self.mr_iid)

    @property
    def mr_summary(self) -> str:
        return (self.scout or {}).get("mr_summary") or ""

    @property
    def file_summaries(self) -> dict[str, str | None]:
        return {f["path"]: f.get("summary")
                for f in ((self.scout or {}).get("files") or [])}

    @property
    def chunks(self) -> list[list[str]]:
        """Changed paths grouped as analyze actually fans them out."""
        sc = self.scout or {}
        by_id = {f["file_id"]: f["path"] for f in (sc.get("files") or [])}
        out = []
        for c in sc.get("chunks") or []:
            paths = [by_id[i] for i in c.get("file_ids", []) if i in by_id]
            if paths:
                out.append(paths)
        return out or [list(self.changed_paths)]


@dataclass
class Strategy:
    name: str
    vector_query: Callable[[MRSnap], str] | None = None
    lexical_query: Callable[[MRSnap], str] | None = None
    pool: int = 40
    lexical_pool: int = 40
    top: int = 8
    explore: bool = True
    weights: tuple[float, float] = (1.0, 1.0)
    per_chunk: bool = False
    # Bonus weights live in the base score's units, so the fused path expresses
    # them as multiples of one top-rank RRF slot.
    pattern_bonus: float | None = None
    strength_weight: float | None = None

    @property
    def fused(self) -> bool:
        return self.vector_query is not None and self.lexical_query is not None


class Corpus:
    """The pinned learning corpus, scoped per repo the way production scopes."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._by_repo: dict[str, list[dict]] = {}
        for r in rows:
            self._by_repo.setdefault(r["repo_id"] or "", []).append(r)

    def scoped(self, repo_id: str) -> list[dict]:
        return self._by_repo.get(repo_id, []) + self._by_repo.get("", [])


def to_learning(row: dict) -> Learning:
    return Learning(
        id=uuid.UUID(row["id"]),
        repo_id=uuid.UUID(row["repo_id"]) if row["repo_id"] else None,
        topic=row["topic"], hint_text=row["hint_text"], kind=row["kind"],
        file_pattern=row["file_pattern"], hit_count=row["hit_count"],
        harmful_count=row["harmful_count"], ignored_count=row["ignored_count"],
        miss_count=row["miss_count"], groundedness=row["groundedness"])


class Embedder:
    """Query embeddings, cached on disk and keyed by text."""

    def __init__(self, cache_path: Path, base_url: str, model: str):
        self.cache_path = cache_path
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.cache: dict[str, list[float]] = {}
        self.truncated: dict[int, int] = {}
        if cache_path.exists():
            self.cache = json.loads(cache_path.read_text())
        self._dirty = False

    def __call__(self, text: str) -> np.ndarray:
        text = (text or "").strip()[:EMBED_INPUT_CEILING]
        key = hashlib.sha256(f"{self.model}|{text}".encode()).hexdigest()
        if key not in self.cache:
            self.cache[key] = self._embed_shrinking(text)
            self._dirty = True
        return np.asarray(self.cache[key], dtype=np.float32)

    def _embed_shrinking(self, text: str) -> list[float]:
        """The server rejects anything over its context window, and the window
        is in tokens -- dense code hits it far sooner than prose. Halve until it
        is accepted rather than guessing a character budget."""
        import httpx
        body = text
        while True:
            prefixed = (body if body.startswith("search_query: ")
                        else f"search_query: {body}")
            resp = httpx.post(f"{self.base_url}/api/embeddings",
                              json={"model": self.model, "prompt": prefixed},
                              timeout=180.0)
            if resp.status_code == 200:
                if len(body) < len(text):
                    self.truncated[len(text)] = len(body)
                return resp.json()["embedding"]
            if resp.status_code != 500 or len(body) <= EMBED_MIN_CHARS:
                resp.raise_for_status()
            body = body[:max(EMBED_MIN_CHARS, len(body) // 2)]

    def flush(self) -> None:
        if self._dirty:
            self.cache_path.write_text(json.dumps(self.cache))
            self._dirty = False


def _cosine_ranked(qvec: np.ndarray, rows: list[dict]) -> list[tuple[str, float]]:
    mat = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
    denom = np.linalg.norm(mat, axis=1) * np.linalg.norm(qvec)
    denom[denom == 0.0] = 1e-9
    sims = (mat @ qvec) / denom
    order = np.argsort(-sims)
    return [(rows[i]["id"], float(sims[i])) for i in order]


def _rank_once(strategy: Strategy, snap: MRSnap, rows: list[dict],
               file_paths: list[str], embed: Embedder) -> list[str]:
    by_id = {r["id"]: r for r in rows}
    vector_ranked: list[tuple[str, float]] = []
    lexical_ranked: list[tuple[str, float]] = []

    if strategy.vector_query is not None:
        qtext = strategy.vector_query(snap)
        if qtext.strip():
            vector_ranked = _cosine_ranked(embed(qtext), rows)
    if strategy.lexical_query is not None:
        qtext = strategy.lexical_query(snap)
        if qtext.strip():
            index = Bm25Index({r["id"]: f'{r["topic"]} :: {r["hint_text"]}'
                               for r in rows})
            lexical_ranked = index.rank(qtext, limit=strategy.lexical_pool)

    vector_ids = [i for i, _ in vector_ranked[:strategy.pool]]
    lexical_ids = [i for i, _ in lexical_ranked]

    if strategy.fused:
        # Union of the two candidate sets, never a rerank of the vector pool:
        # a lexical winner outside the vector top-N is the whole point.
        cand_ids = list(dict.fromkeys(vector_ids + lexical_ids))
        base = hybrid_base_scores(
            [uuid.UUID(i) for i in vector_ids],
            [uuid.UUID(i) for i in lexical_ids],
            weights=strategy.weights)
        pattern_bonus = (strategy.pattern_bonus
                         if strategy.pattern_bonus is not None else RRF_UNIT)
        strength_weight = (strategy.strength_weight
                           if strategy.strength_weight is not None else 0.5 * RRF_UNIT)
    elif strategy.vector_query is not None:
        cand_ids = vector_ids
        base = {uuid.UUID(i): s for i, s in vector_ranked[:strategy.pool]}
        pattern_bonus, strength_weight = 0.3, 0.25
    else:
        cand_ids = lexical_ids
        top_score = max((s for _, s in lexical_ranked), default=1.0) or 1.0
        base = {uuid.UUID(i): s / top_score for i, s in lexical_ranked}
        pattern_bonus, strength_weight = 0.3, 0.25

    candidates = [to_learning(by_id[i]) for i in cand_ids]
    chosen = rank_learnings(candidates, base, file_paths=file_paths,
                            top=strategy.top, explore=strategy.explore,
                            pattern_bonus=pattern_bonus,
                            strength_weight=strength_weight)
    return [str(l.id)[:8] for l in chosen]


def run_strategy(strategy: Strategy, snaps: list[MRSnap], corpus: Corpus,
                 gold: dict, embed: Embedder) -> tuple[list[MRResult], dict[int, list[str]]]:
    results, picked = [], {}
    for snap in snaps:
        if snap.key not in gold:
            continue
        rows = corpus.scoped(snap.repo_id)
        if strategy.per_chunk:
            ranked: list[str] = []
            for paths in snap.chunks:
                sub = MRSnap(snap.project_path, snap.mr_iid, snap.repo_id,
                             snap.title, paths,
                             snap.added_lines, _scout_for(snap, paths))
                for lid in _rank_once(strategy, sub, rows, paths, embed):
                    if lid not in ranked:
                        ranked.append(lid)
        else:
            ranked = _rank_once(strategy, snap, rows, snap.changed_paths, embed)
        picked[snap.mr_iid] = ranked
        k = None if strategy.per_chunk else strategy.top
        results.append(score_ranking(gold[snap.key], ranked, k=k))
    return results, picked


def _scout_for(snap: MRSnap, paths: list[str]) -> dict | None:
    """Scout artifact narrowed to one chunk's files."""
    if not snap.scout:
        return None
    keep = set(paths)
    out = dict(snap.scout)
    out["files"] = [f for f in (snap.scout.get("files") or [])
                    if f.get("path") in keep]
    return out


def build_strategies() -> dict[str, Strategy]:
    baseline_q = lambda s: learnings_query_text(s.title, s.changed_paths, s.added_lines)
    fulldiff_q = lambda s: learnings_query_text(s.title, s.changed_paths,
                                                s.added_lines, max_chars=100_000)
    ident_q = lambda s: identifier_query(s.added_lines)
    scout_q = lambda s: scout_query(s.mr_summary, s.file_summaries, s.changed_paths)
    scout_ident_q = lambda s: f"{scout_q(s)}\n{ident_q(s)}"

    out = [
        Strategy("baseline", vector_query=baseline_q),
        Strategy("baseline_noexplore", vector_query=baseline_q, explore=False),
        Strategy("fulldiff", vector_query=fulldiff_q),
        Strategy("identifiers", vector_query=ident_q),
        Strategy("scout", vector_query=scout_q),
        Strategy("lexical", lexical_query=ident_q),
        Strategy("lexical_scout", lexical_query=scout_ident_q),
        Strategy("hybrid", vector_query=baseline_q, lexical_query=ident_q),
        Strategy("hybrid_scout", vector_query=scout_q, lexical_query=scout_ident_q),
        Strategy("hybrid_scout_noexplore", vector_query=scout_q,
                 lexical_query=scout_ident_q, explore=False),
        Strategy("hybrid_perchunk", vector_query=scout_q,
                 lexical_query=scout_ident_q, per_chunk=True),
        # Mirrors what production runs when settings.retrieval_hybrid is on:
        # vector arm on the historical query, lexical arm on scout's summaries.
        Strategy("shipped", vector_query=baseline_q, lexical_query=scout_q,
                 pattern_bonus=HYBRID_PATTERN_BONUS,
                 strength_weight=HYBRID_STRENGTH_WEIGHT),
    ]
    return {s.name: s for s in out}


def known_miss_report(picked: dict[int, list[str]]) -> str:
    lines, hit, total = [], 0, 0
    for iid, ids in sorted(KNOWN_MISSES.items()):
        got = [i for i in ids if i in set(picked.get(iid, []))]
        hit += len(got)
        total += len(ids)
        marks = " ".join(f"{'+' if i in got else '-'}{i}" for i in ids)
        lines.append(f"    !{iid}: {marks}")
    return f"  known misses recovered: {hit}/{total}\n" + "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--strategies", default="baseline")
    p.add_argument("--cache", default="embed_cache.json")
    p.add_argument("--ollama", default=os.environ.get(
        "ARGUS_OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    p.add_argument("--model", default="nomic-embed-text")
    p.add_argument("--known-misses", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING)

    raw = json.loads(Path(args.snapshot).read_text())
    corpus = Corpus(raw["learnings"])
    snaps = [MRSnap(m["project_path"], m["mr_iid"], m["repo_id"], m["title"],
                    m["changed_paths"], m["added_lines"], m["scout"])
             for m in raw["mrs"]]
    gold = load_gold()
    embed = Embedder(Path(args.cache), args.ollama, args.model)
    available = build_strategies()

    names = [n.strip() for n in args.strategies.split(",") if n.strip()]
    if names == ["all"]:
        names = list(available)
    for name in names:
        if name not in available:
            raise SystemExit(f"unknown strategy {name}; have {', '.join(available)}")
        results, picked = run_strategy(available[name], snaps, corpus, gold, embed)
        embed.flush()
        report(name, results)
        if args.known_misses:
            print(known_miss_report(picked))


if __name__ == "__main__":
    main()
