import httpx
import pytest
from sqlalchemy import delete

from argus.api.app import create_app
from argus.domain.models import RuntimeSetting


@pytest.fixture
async def api(engine, settings):
    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    async with engine.begin() as conn:
        await conn.execute(delete(RuntimeSetting))


async def test_get_settings_masks_secrets(api):
    r = await api.get("/settings")
    assert r.status_code == 200
    body = r.json()
    assert "gitlab_token" not in body["values"]
    assert body["secrets"]["gitlab_token"] is True   # conftest sets gitlab_token="t"
    assert body["values"]["max_inline_comments"] == 5


async def test_put_settings_roundtrip(api):
    r = await api.put("/settings", json={"values": {"max_inline_comments": 9,
                                                    "poller_enabled": True}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["values"]["max_inline_comments"] == 9
    assert "max_inline_comments" in body["overridden"]
    r = await api.get("/settings")
    assert r.json()["values"]["max_inline_comments"] == 9


async def test_put_settings_rejects_secret(api):
    r = await api.put("/settings", json={"values": {"gitlab_token": "sneaky"}})
    assert r.status_code == 422
    r = await api.put("/settings", json={"values": {"poll_interval_s": "abc"}})
    assert r.status_code == 422


async def test_put_settings_rejects_poll_interval_below_floor(api):
    r = await api.put("/settings", json={"values": {"poll_interval_s": 0}})
    assert r.status_code == 422
    assert "poll_interval_s" in r.text


def test_every_review_tuning_field_in_the_ui_is_a_real_editable_setting():
    """`design_max_rounds` shipped in this section's FIELDS but existed nowhere
    in the backend -- not in Settings, not in EDITABLE_KEYS. It rendered as a
    blank input and any edit 400'd with "unknown setting". Reviewer agents take
    their rounds from ReviewerAgentVersion.max_rounds, so there was never a
    global setting for it to bind to."""
    import re
    from pathlib import Path

    from argus.settings_store import EDITABLE_KEYS

    section = (Path(__file__).parent.parent / "frontend/src/features/settings"
               / "sections/ReviewTuningSection.tsx").read_text()
    fields = section[section.index("const FIELDS = ["):section.index("] as const;")]
    keys = re.findall(r'key:\s*"([a-z0-9_]+)"', fields)

    assert keys, "could not parse FIELDS out of ReviewTuningSection.tsx"
    unknown = [k for k in keys if k not in EDITABLE_KEYS]
    assert unknown == [], f"UI offers settings the backend rejects: {unknown}"


def test_distiller_round_budget_is_editable_with_a_floor():
    from argus.settings_store import EDITABLE_KEYS, EDITABLE_MINIMUMS

    assert EDITABLE_KEYS["distiller_max_rounds"] is int
    assert EDITABLE_MINIMUMS["distiller_max_rounds"] >= 1, \
        "zero rounds would distil nothing at all"
