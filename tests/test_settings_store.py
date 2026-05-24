import pytest

from argus.config import Settings
from argus.settings_store import (EDITABLE_KEYS, SECRET_KEYS,
                                      apply_settings, load_effective_settings,
                                      read_settings_view)


def test_secrets_are_not_editable():
    assert set(SECRET_KEYS) == {"gitlab_token", "api_token", "distiller_api_key",
                                "langfuse_public_key", "langfuse_secret_key"}
    assert not set(SECRET_KEYS) & set(EDITABLE_KEYS)
    # database_url and workspace_root are infra, not runtime-tunable
    assert "database_url" not in EDITABLE_KEYS
    assert "workspace_root" not in EDITABLE_KEYS


async def test_apply_then_load_overlays_env(db):
    base = Settings(poll_interval_s=120, max_inline_comments=5)
    await apply_settings(db, {"poll_interval_s": 60, "max_inline_comments": 9})
    await db.flush()
    eff = await load_effective_settings(db, base=base)
    assert eff.poll_interval_s == 60
    assert eff.max_inline_comments == 9
    assert eff.scout_max_rounds == base.scout_max_rounds  # untouched key


async def test_apply_rejects_secret_and_unknown_keys(db):
    with pytest.raises(ValueError, match="gitlab_token"):
        await apply_settings(db, {"gitlab_token": "x"})
    with pytest.raises(ValueError, match="nonsense"):
        await apply_settings(db, {"nonsense": 1})
    with pytest.raises(ValueError, match="poll_interval_s"):
        await apply_settings(db, {"poll_interval_s": "not-an-int"})


async def test_apply_rejects_poll_interval_below_floor(db):
    with pytest.raises(ValueError, match="poll_interval_s"):
        await apply_settings(db, {"poll_interval_s": 5})
    with pytest.raises(ValueError, match="poll_interval_s"):
        await apply_settings(db, {"poll_interval_s": 0})


async def test_apply_accepts_poll_interval_at_floor(db):
    await apply_settings(db, {"poll_interval_s": 10})
    await db.flush()
    eff = await load_effective_settings(db, base=Settings())
    assert eff.poll_interval_s == 10


async def test_apply_rejects_review_token_ceiling_below_floor(db):
    with pytest.raises(ValueError, match="review_token_ceiling"):
        await apply_settings(db, {"review_token_ceiling": 100})


async def test_apply_rejects_zero_max_rounds(db):
    with pytest.raises(ValueError, match="scout_max_rounds"):
        await apply_settings(db, {"scout_max_rounds": 0})
    with pytest.raises(ValueError, match="analysis_max_rounds"):
        await apply_settings(db, {"analysis_max_rounds": 0})
    with pytest.raises(ValueError, match="verify_max_rounds"):
        await apply_settings(db, {"verify_max_rounds": 0})


async def test_apply_rejects_zero_max_chunk_files(db):
    with pytest.raises(ValueError, match="max_chunk_files"):
        await apply_settings(db, {"max_chunk_files": 0})


async def test_apply_rejects_negative_max_inline_comments(db):
    with pytest.raises(ValueError, match="max_inline_comments"):
        await apply_settings(db, {"max_inline_comments": -1})


async def test_apply_accepts_zero_max_inline_comments(db):
    """Zero is a legitimate 'post no comments' configuration."""
    await apply_settings(db, {"max_inline_comments": 0})
    await db.flush()
    eff = await load_effective_settings(db, base=Settings())
    assert eff.max_inline_comments == 0


async def test_read_settings_view_shape(db):
    base = Settings(gitlab_token="tok", gitlab_url="https://gl.test")
    await apply_settings(db, {"log_level": "DEBUG"})
    await db.flush()
    view = await read_settings_view(db, base)
    assert view["values"]["log_level"] == "DEBUG"
    assert view["values"]["gitlab_url"] == "https://gl.test"
    assert "gitlab_token" not in view["values"]
    assert view["secrets"]["gitlab_token"] is True
    assert view["secrets"]["distiller_api_key"] is False
    assert "log_level" in view["overridden"]


async def test_effective_settings_used_at_job_start(db):
    """A DB override applied before a job starts is visible in the loaded settings."""
    base = Settings(review_token_ceiling=500_000)
    await apply_settings(db, {"review_token_ceiling": 100_000})
    await db.flush()
    eff = await load_effective_settings(db, base=base)
    assert eff.review_token_ceiling == 100_000


def test_design_max_rounds_removed_from_editable_keys():
    from argus.settings_store import EDITABLE_KEYS
    assert "design_max_rounds" not in EDITABLE_KEYS
