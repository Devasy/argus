import re

from argus.review.artifacts import Chunk, FileChange

RISK_RULES: list[tuple[str, str]] = [
    (r"(auth|token|secret|password|crypt)", "security"),
    (r"(migration|schema|models)", "schema"),
    (r"(api|routes|views)", "api_surface"),
    (r"Dockerfile|docker-compose|\.ya?ml$", "infra"),
]
_LANG_LENS = {"python": "python_best_practices", "react": "react_best_practices",
              "typescript": "react_best_practices"}


def apply_risk_flags(files: list[FileChange]) -> list[FileChange]:
    for f in files:
        for pattern, flag in RISK_RULES:
            if re.search(pattern, f.path, re.I) and flag not in f.risk_flags:
                f.risk_flags.append(flag)
    return files


def _lenses(files: list[FileChange]) -> list[str]:
    lenses: list[str] = ["correctness"]
    for f in files:
        lang_lens = _LANG_LENS.get(f.language or "")
        if lang_lens and lang_lens not in lenses:
            lenses.append(lang_lens)
        if "security" in f.risk_flags and "security" not in lenses:
            lenses.append("security")
    return lenses


CHARS_PER_TOKEN = 4
HUNK_OVERHEAD_TOKENS = 30
FILE_FLOOR_TOKENS = 200


def hunk_tokens(h) -> int:
    return len(h.diff_text or "") // CHARS_PER_TOKEN + HUNK_OVERHEAD_TOKENS


def file_hunks(f: FileChange, hunks: dict) -> list:
    return sorted((hunks[h] for h in f.hunk_ids if h in hunks), key=lambda h: h.new_start)


def split_hunks(file_hunk_list: list, budget: int) -> list[list]:
    """Consecutive hunk groups each within budget; a single oversized hunk gets a group of its own."""
    groups, cur, cur_tok = [], [], 0
    for h in file_hunk_list:
        t = hunk_tokens(h)
        if cur and cur_tok + t > budget:
            groups.append(cur)
            cur, cur_tok = [], 0
        cur.append(h)
        cur_tok += t
    if cur:
        groups.append(cur)
    return groups


def plan_chunks(files: list[FileChange], max_chunk_files: int, hunks: dict | None = None,
                token_budget: int = 0) -> list[Chunk]:
    """Chunks of at most max_chunk_files files and (when hunks + budget are given) ~token_budget
    tokens of diff; a file bigger than the budget is split into consecutive hunk groups."""
    sized = bool(hunks) and token_budget > 0
    groups: dict[str, list[FileChange]] = {}
    for f in files:
        top = f.path.split("/", 1)[0]
        groups.setdefault(top, []).append(f)
    chunks: list[Chunk] = []

    def emit(batch: list[FileChange], hunk_ids=None, part=None):
        chunks.append(Chunk(chunk_id=f"c{len(chunks) + 1}", file_ids=[f.file_id for f in batch],
                            lenses=_lenses(batch), hunk_ids=hunk_ids, part=part))

    for _, members in sorted(groups.items()):
        if not sized:
            for i in range(0, len(members), max_chunk_files):
                emit(members[i:i + max_chunk_files])
            continue
        cur: list[FileChange] = []
        cur_tok = 0
        for f in members:
            fh = file_hunks(f, hunks)
            ft = max(FILE_FLOOR_TOKENS, sum(hunk_tokens(h) for h in fh))
            if ft > token_budget and len(fh) > 1:
                if cur:
                    emit(cur)
                    cur, cur_tok = [], 0
                parts = split_hunks(fh, token_budget)
                for i, grp in enumerate(parts, start=1):
                    emit([f], hunk_ids=[h.hunk_id for h in grp], part=f"{i}/{len(parts)}")
                continue
            if cur and (len(cur) >= max_chunk_files or cur_tok + ft > token_budget):
                emit(cur)
                cur, cur_tok = [], 0
            cur.append(f)
            cur_tok += ft
        if cur:
            emit(cur)

    whole = all(c.hunk_ids is None for c in chunks)
    total_tok = (sum(max(FILE_FLOOR_TOKENS, sum(hunk_tokens(h) for h in file_hunks(f, hunks)))
                     for f in files) if sized else 0)
    if (len(chunks) > 1 and whole and sum(len(c.file_ids) for c in chunks) <= max_chunk_files
            and (not sized or total_tok <= token_budget)):
        return [Chunk(chunk_id="c1", file_ids=[fid for c in chunks for fid in c.file_ids],
                      lenses=sorted({l for c in chunks for l in c.lenses}))]
    return chunks
