import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from argus.api.permissions import PERMISSIONS, permissions_for, require_permission


def _app(settings):
    from fastapi import Depends
    app = FastAPI()
    app.state.settings = settings

    @app.post("/x")
    def x(p=Depends(require_permission("audit.approve"))):
        return {"label": p.label}
    return app


def test_every_permission_maps_to_a_known_role():
    assert set(PERMISSIONS.values()) <= {"user", "admin"}
    assert PERMISSIONS["audit.approve"] == "admin"
    assert PERMISSIONS["audit.run"] == "admin"


def test_permissions_for_roles_are_cumulative():
    assert "audit.approve" in permissions_for("admin")
    assert "audit.approve" not in permissions_for("user")
    assert set(permissions_for("user")) <= set(permissions_for("admin"))


def test_require_permission_blocks_a_user_token(settings):
    s = settings.model_copy(update={"api_token": "u", "admin_token": "a"})
    c = TestClient(_app(s))
    assert c.post("/x", headers={"Authorization": "Bearer u"}).status_code == 403
    assert c.post("/x", headers={"Authorization": "Bearer a"}).status_code == 200


def test_unknown_permission_is_a_programming_error():
    with pytest.raises(KeyError):
        require_permission("nope.nothing")
