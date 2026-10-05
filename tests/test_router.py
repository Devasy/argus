from argus.review.artifacts import FileChange
from argus.review.router import apply_risk_flags, plan_chunks


def _f(i, path, kind="modified", lang="python"):
    return FileChange(file_id=f"f{i}", path=path, change_kind=kind,
                      language=lang, hunk_ids=[f"h{i}"])


def test_risk_flags():
    files = apply_risk_flags([_f(1, "app/auth/token.py"), _f(2, "app/models.py")])
    assert "security" in files[0].risk_flags
    assert "schema" in files[1].risk_flags


def test_small_mr_single_chunk():
    files = [_f(1, "a/x.py"), _f(2, "a/y.py")]
    chunks = plan_chunks(files, max_chunk_files=8)
    assert len(chunks) == 1 and chunks[0].chunk_id == "c1"
    assert "python_best_practices" in chunks[0].lenses


def test_large_mr_grouped_by_dir():
    files = [_f(i, f"pkg{i % 3}/m{i}.py") for i in range(1, 13)]
    chunks = plan_chunks(files, max_chunk_files=4)
    assert len(chunks) >= 3
    assert all(len(c.file_ids) <= 4 for c in chunks)


def test_security_lens_added():
    files = apply_risk_flags([_f(1, "svc/auth/login.py")])
    chunks = plan_chunks(files, max_chunk_files=8)
    assert "security" in chunks[0].lenses


# ---- size-aware chunking -------------------------------------------------------------------

from types import SimpleNamespace

from argus.review.artifacts import Chunk, Hunk
from argus.review.pipeline import chunk_scope_note, chunk_token_budget


def _sized(path, fid, n_hunks, chars):
    f = FileChange(file_id=fid, path=path, change_kind="modified", language="python",
                   hunk_ids=[f"{fid}h{i}" for i in range(n_hunks)])
    hunks = {h: Hunk(hunk_id=h, file_id=fid, old_start=i * 100 + 1, old_lines=10,
                     new_start=i * 100 + 1, new_lines=10, diff_text="x" * chars)
             for i, h in enumerate(f.hunk_ids)}
    return f, hunks


def test_one_huge_file_is_split_into_ordered_hunk_parts():
    f, hunks = _sized("plugin/main.py", "f1", 10, 4000)
    chunks = plan_chunks([f], max_chunk_files=8, hunks=hunks, token_budget=3000)
    assert [c.part for c in chunks] == ["1/5", "2/5", "3/5", "4/5", "5/5"]
    assert all(c.file_ids == ["f1"] for c in chunks)
    assert [h for c in chunks for h in c.hunk_ids] == f.hunk_ids


def test_small_files_pack_under_the_budget_and_big_file_stands_alone():
    small = [_sized(f"a/s{i}.py", f"s{i}", 1, 400) for i in range(3)]
    big, big_h = _sized("a/big.py", "big", 4, 8000)
    files = [s[0] for s in small] + [big]
    hunks = {k: v for _, h in small for k, v in h.items()} | big_h
    chunks = plan_chunks(files, max_chunk_files=8, hunks=hunks, token_budget=2500)
    whole = [c for c in chunks if c.hunk_ids is None]
    parts = [c for c in chunks if c.hunk_ids]
    assert [c.file_ids for c in whole] == [["s0", "s1", "s2"]]
    assert len(parts) == 4 and all(c.file_ids == ["big"] for c in parts)


def test_small_mr_still_one_chunk_when_sized():
    a, ha = _sized("a/x.py", "x", 2, 400)
    b, hb = _sized("b/y.py", "y", 1, 400)
    chunks = plan_chunks([a, b], max_chunk_files=8, hunks=ha | hb, token_budget=5000)
    assert len(chunks) == 1 and chunks[0].hunk_ids is None


def test_a_single_oversized_hunk_is_not_split():
    f, hunks = _sized("plugin/main.py", "f1", 1, 40000)
    chunks = plan_chunks([f], max_chunk_files=8, hunks=hunks, token_budget=3000)
    assert len(chunks) == 1 and chunks[0].hunk_ids is None


def test_scope_note_names_hunks_and_lines():
    f, hunks = _sized("plugin/main.py", "f1", 3, 10)
    note = chunk_scope_note(Chunk(chunk_id="c2", file_ids=["f1"], lenses=[],
                                  hunk_ids=["f1h1"], part="2/3"), hunks)
    assert "PART 2/3" in note and "f1h1 (new lines 101-110)" in note
    assert chunk_scope_note(Chunk(chunk_id="c1", file_ids=["f1"], lenses=[]), hunks) == ""


def test_budget_is_explicit_or_a_fifth_of_the_window():
    assert chunk_token_budget(SimpleNamespace(chunk_token_budget=9000,
                                              model_context_window=131072)) == 9000
    assert chunk_token_budget(SimpleNamespace(chunk_token_budget=0,
                                              model_context_window=131072)) == 26214
    assert chunk_token_budget(SimpleNamespace(chunk_token_budget=0,
                                              model_context_window=8000)) == 4000
