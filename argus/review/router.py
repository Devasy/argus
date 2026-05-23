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


def plan_chunks(files: list[FileChange], max_chunk_files: int) -> list[Chunk]:
    groups: dict[str, list[FileChange]] = {}
    for f in files:
        top = f.path.split("/", 1)[0]
        groups.setdefault(top, []).append(f)
    chunks: list[Chunk] = []
    seq = 0
    for _, members in sorted(groups.items()):
        for i in range(0, len(members), max_chunk_files):
            batch = members[i:i + max_chunk_files]
            seq += 1
            chunks.append(Chunk(chunk_id=f"c{seq}",
                                file_ids=[f.file_id for f in batch],
                                lenses=_lenses(batch)))
    if len(chunks) > 1 and sum(len(c.file_ids) for c in chunks) <= max_chunk_files:
        merged = Chunk(chunk_id="c1",
                       file_ids=[fid for c in chunks for fid in c.file_ids],
                       lenses=sorted({l for c in chunks for l in c.lenses}))
        return [merged]
    return chunks
