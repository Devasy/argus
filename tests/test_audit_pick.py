import subprocess
import uuid
from datetime import datetime, timedelta, timezone

from argus.domain.models import Learning, Repository
from argus.knowledge.audit_pick import changed_paths_since, count_due, select_due_learnings


def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo_with_two_commits(tmp_path):
    r = tmp_path / "w"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.py").write_text("x = 1\n")
    (r / "b.py").write_text("y = 1\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "one")
    first = _git(r, "rev-parse", "HEAD")
    (r / "a.py").write_text("x = 2\n")
    _git(r, "commit", "-qam", "two")
    return r, first


def test_changed_paths_since(tmp_path):
    r, first = _repo_with_two_commits(tmp_path)
    assert changed_paths_since(r, first) == {"a.py"}
    assert changed_paths_since(r, "0" * 40) is None
    assert changed_paths_since(r, None) is None


async def test_due_order_never_then_changed_then_old(db, tmp_path):
    r, first = _repo_with_two_commits(tmp_path)
    repo = Repository(provider="gitlab", project_path=f"g/pk-{uuid.uuid4().hex[:6]}",
                      gitlab_project_id=int(uuid.uuid4().int % 10**8))
    db.add(repo)
    await db.flush()
    now = datetime.now(timezone.utc)
    mk = lambda **kw: Learning(repo_id=repo.id, topic="t", hint_text="h", kind="guidance", **kw)
    fresh_ok = mk(last_audited_at=now - timedelta(days=1), audited_at_sha=first, file_paths=["b.py"])
    changed = mk(last_audited_at=now - timedelta(days=1), audited_at_sha=first, file_paths=["a.py"])
    old = mk(last_audited_at=now - timedelta(days=30))
    never = mk()
    db.add_all([fresh_ok, changed, old, never])
    await db.flush()

    due = await select_due_learnings(db, repo.id, workspace=r, now=now,
                                     reaudit_after_days=7, limit=10)
    assert [l.id for l in due] == [never.id, changed.id, old.id]
    assert (await select_due_learnings(db, repo.id, workspace=r, now=now,
                                       reaudit_after_days=7, limit=2))[-1].id == changed.id
    assert await count_due(db, repo.id, now=now, reaudit_after_days=7) == 2
