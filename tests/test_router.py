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
