import uuid
from datetime import datetime, timezone

import pytest

from argus.db import session_factory
from argus.domain.models import (LLMRound, MergeRequest, Repository,
                                     Review)
from argus.review.runner import execute_review_job


async def test_execute_review_job_aggregates_llm_round_tokens_on_completion(
        db, engine, settings, monkeypatch):
    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/runner-agg-test",
                          gitlab_project_id=90000600)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=9, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="queued", trigger="manual",
                        llm_config={"provider": "anthropic", "model": "m",
                                   "temperature": 0.1})
        s.add(review); await s.flush()
        review_id = review.id
        await s.commit()

    async def fake_get_merge_request(self, project_id, mr_iid):
        raise RuntimeError("simulated gitlab failure — no network in this test")

    from argus.gitlab.client import GitLabClient
    monkeypatch.setattr(GitLabClient, "get_merge_request", fake_get_merge_request)

    # Simulate LLMRound rows that would have been written by DBTraceCallback
    # during a real pipeline run, before the (simulated) failure.
    async with sf() as s:
        s.add(LLMRound(review_id=review_id, stage_name="scout", seq=1,
                       prompt_tokens=200, completion_tokens=80))
        s.add(LLMRound(review_id=review_id, stage_name="scout", seq=2,
                       prompt_tokens=50, completion_tokens=20))
        await s.commit()

    with pytest.raises(RuntimeError, match="simulated gitlab failure"):
        await execute_review_job(sf, settings, {"review_id": str(review_id)})

    async with sf() as s:
        updated = await s.get(Review, review_id)
        assert updated.status == "failed"
        assert updated.prompt_tokens == 250
        assert updated.completion_tokens == 100


# --- learnings query text: real code content, not just paths --------------
#
# Measured against production data (review-quality ledger, item 6): a query
# built from MR title + bare file paths gives near-chance retrieval
# separation for learning relevance (AUC 0.564 across hit/miss/ignored
# outcomes). The candidate fix was to enrich the query with actual diff
# content before embedding.

def test_diff_signal_includes_added_lines_only():
    from argus.review.artifacts import Hunk
    from argus.review.runner import _diff_signal

    hunks = {"h1": Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=2,
                        new_start=1, new_lines=3, diff_text=(
                            "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n"
                            " unchanged_context_line\n"
                            "-removed_old_code\n"
                            "+added_new_code_signal\n"))}
    signal = _diff_signal(hunks)
    assert "added_new_code_signal" in signal
    assert "removed_old_code" not in signal
    assert "unchanged_context_line" not in signal
    assert "+++ b/x.py" not in signal


def test_diff_signal_is_bounded(monkeypatch):
    from argus.review.artifacts import Hunk
    from argus.review.runner import _diff_signal

    big = "\n".join(f"+line_{i}_padding_padding_padding" for i in range(500))
    hunks = {"h1": Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                        new_start=1, new_lines=500, diff_text=big)}
    signal = _diff_signal(hunks, max_chars=100)
    assert len(signal) <= 100


def test_diff_signal_empty_for_no_hunks():
    from argus.review.runner import _diff_signal
    assert _diff_signal({}) == ""


def test_learnings_query_text_composes_title_paths_and_diff_signal():
    from argus.review.artifacts import Hunk
    from argus.review.runner import _learnings_query_text

    hunks = {"h1": Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                        new_start=1, new_lines=1,
                        diff_text="+def handle_price_update():\n")}
    q = _learnings_query_text("Bump the model price", ["billing/rates.py"], hunks)
    assert "Bump the model price" in q
    assert "billing/rates.py" in q
    assert "handle_price_update" in q


def test_learnings_query_text_falls_back_to_title_and_paths_when_no_diff():
    """A binary file, or a file GitLab collapsed with no recoverable hunks,
    must still produce a usable query -- the old behavior, unchanged."""
    from argus.review.runner import _learnings_query_text

    q = _learnings_query_text("Add a new asset", ["assets/logo.png"], {})
    assert q == "Add a new asset assets/logo.png"


def test_learnings_query_text_caps_the_joined_paths(monkeypatch):
    """54 renamed files with long nested paths (a real production MR) built a
    5,390-char query with no diff content at all -- entirely from paths -- and
    that alone exceeded the embedding model's real context window and crashed
    the whole review before the pipeline started. The paths portion needs the
    same bound _diff_signal already has."""
    from argus.review.runner import _learnings_query_text

    paths = [f"very/long/nested/directory/structure/path/number_{i}/file.txt"
             for i in range(200)]
    q = _learnings_query_text("Bump the model price", paths, {})
    assert len(q) < 4000
