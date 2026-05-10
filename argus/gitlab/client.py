"""Single home for every GitLab REST call argus makes."""
import urllib.parse
from typing import Any

import httpx

MAX_PAGES = 20
PER_PAGE = 100


class GitLabClient:
    def __init__(self, base_url: str, token: str, ssl_verify: bool | str = True):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v4",
            headers={"PRIVATE-TOKEN": token},
            verify=ssl_verify,
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _proj(project) -> str:
        return urllib.parse.quote(str(project), safe="")

    async def _get(self, path: str, **params) -> Any:
        r = await self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    async def _get_paginated(self, path: str, **params) -> list[dict]:
        params = {k: v for k, v in params.items() if v is not None}
        params["per_page"] = PER_PAGE
        out: list[dict] = []
        page = 1
        while page <= MAX_PAGES:
            r = await self._http.get(path, params={**params, "page": page})
            r.raise_for_status()
            out.extend(r.json())
            if not r.headers.get("X-Next-Page"):
                break
            page += 1
        return out

    async def _post(self, path: str, payload: dict) -> dict:
        r = await self._http.post(path, json=payload)
        r.raise_for_status()
        return r.json()

    # ---- reads
    async def get_current_user(self) -> dict:
        return await self._get("/user")

    async def get_project(self, project) -> dict:
        return await self._get(f"/projects/{self._proj(project)}")

    async def list_merge_requests(self, project, updated_after: str | None = None,
                                  state: str = "all") -> list[dict]:
        return await self._get_paginated(
            f"/projects/{self._proj(project)}/merge_requests",
            updated_after=updated_after, state=state, order_by="updated_at")

    async def get_merge_request(self, project, iid) -> dict:
        return await self._get(f"/projects/{self._proj(project)}/merge_requests/{iid}")

    async def list_discussions(self, project, iid) -> list[dict]:
        return await self._get_paginated(
            f"/projects/{self._proj(project)}/merge_requests/{iid}/discussions")

    async def list_notes(self, project, iid) -> list[dict]:
        return await self._get_paginated(
            f"/projects/{self._proj(project)}/merge_requests/{iid}/notes",
            sort="asc", order_by="created_at")

    async def list_versions(self, project, iid) -> list[dict]:
        return await self._get(f"/projects/{self._proj(project)}/merge_requests/{iid}/versions")

    async def list_diffs(self, project, iid) -> list[dict]:
        return await self._get_paginated(
            f"/projects/{self._proj(project)}/merge_requests/{iid}/diffs")

    async def get_approvals(self, project, iid) -> dict:
        return await self._get(f"/projects/{self._proj(project)}/merge_requests/{iid}/approvals")

    async def get_reviewers(self, project, iid) -> list[dict]:
        return await self._get(f"/projects/{self._proj(project)}/merge_requests/{iid}/reviewers")

    async def list_participants(self, project, iid) -> list[dict]:
        return await self._get_paginated(
            f"/projects/{self._proj(project)}/merge_requests/{iid}/participants")

    async def get_note_awards(self, project, iid, note_id) -> list[dict]:
        return await self._get(
            f"/projects/{self._proj(project)}/merge_requests/{iid}/notes/{note_id}/award_emoji")

    async def compare(self, project, frm: str, to: str) -> dict:
        return await self._get(f"/projects/{self._proj(project)}/repository/compare",
                               **{"from": frm, "to": to})

    async def get_file_raw(self, project, path: str, ref: str) -> str:
        r = await self._http.get(
            f"/projects/{self._proj(project)}/repository/files/"
            f"{urllib.parse.quote(path, safe='')}/raw", params={"ref": ref})
        r.raise_for_status()
        return r.text

    # ---- writes
    async def create_discussion(self, project, iid, body: str,
                                position: dict | None = None) -> dict:
        payload: dict = {"body": body}
        if position:
            payload["position"] = position
        return await self._post(
            f"/projects/{self._proj(project)}/merge_requests/{iid}/discussions", payload)

    async def create_note(self, project, iid, discussion_id: str, body: str) -> dict:
        return await self._post(
            f"/projects/{self._proj(project)}/merge_requests/{iid}"
            f"/discussions/{discussion_id}/notes", {"body": body})

    async def resolve_discussion(self, project, iid, discussion_id: str,
                                 resolved: bool) -> dict:
        r = await self._http.put(
            f"/projects/{self._proj(project)}/merge_requests/{iid}"
            f"/discussions/{discussion_id}", json={"resolved": resolved})
        r.raise_for_status()
        return r.json()
