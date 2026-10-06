"""GitHub REST/GraphQL adapter; all GitHub wire formats stop here."""
import base64
import re
from urllib.parse import quote, urlparse

import httpx

from argus.providers.base import Capabilities

API = "https://api.github.com"
MAX_PAGES = 100


def user(raw):
    return {"id": raw["id"], "username": raw.get("login", ""),
            "name": raw.get("name"), "avatar_url": raw.get("avatar_url"),
            "bot": raw.get("type") == "Bot"}


def note(raw, kind="review_comment"):
    pos = None
    if raw.get("path"):
        pos = {"new_path": raw["path"], "old_path": raw["path"],
               "new_line": raw.get("line") or raw.get("original_line"),
               "head_sha": raw.get("original_commit_id") or raw.get("commit_id"),
               "side": raw.get("side", "RIGHT"),
               "start_line": raw.get("start_line"), "start_side": raw.get("start_side")}
    return {"id": raw["id"], "provider_note_key": f"{kind}:{raw['id']}",
            "body": raw.get("body") or "", "author": user(raw["user"]),
            "created_at": raw.get("created_at") or raw.get("submitted_at"),
            "position": pos, "type": "DiffNote" if pos else "Note",
            "reply_to_id": raw.get("in_reply_to_id"), "raw": raw}


def diff(raw):
    return {"old_path": raw.get("previous_filename") or raw["filename"],
            "new_path": raw["filename"], "diff": raw.get("patch") or "",
            "new_file": raw["status"] == "added", "deleted_file": raw["status"] == "removed",
            "renamed_file": raw["status"] == "renamed", "raw": raw}


class GitHubProvider:
    provider_name = "github"
    capabilities = Capabilities(selected_requests=True, review_versions=False)

    def __init__(self, settings, repo=None):
        self.settings, self.repo = settings, repo
        from argus.providers.github_auth import InstallationAuth
        self.auth = InstallationAuth(settings) if settings.github_app_id else None
        if self.auth and not (settings.github_installation_id and settings.github_app_private_key_path):
            raise ValueError("GitHub App requires installation ID and private key path")
        self._thread_roots = {}
        self._http = httpx.AsyncClient(base_url=API, timeout=30,
            headers={"Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     **({"Authorization": f"Bearer {settings.github_token}"}
                        if settings.github_token else {})})

    async def aclose(self):
        await self._http.aclose()

    def clone_url(self, project_path):
        # OSS cloning is public; installation credentials stay in the API transport.
        if self.auth:
            return f"https://github.com/{project_path}.git"
        token = quote(self.settings.github_token, safe="")
        auth = f"x-access-token:{token}@" if token else ""
        return f"https://{auth}github.com/{project_path}.git"

    def review_ref(self, number):
        return f"refs/pull/{number}/head"

    def _path(self, project, suffix=""):
        if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", project):
            raise ValueError("GitHub repository must be owner/repo")
        return f"/repos/{project}{suffix}"

    async def _request(self, method, path, **kwargs):
        if self.auth:
            kwargs["headers"] = {**kwargs.get("headers", {}), "Authorization": f"Bearer {await self.auth.token()}"}
        r = await self._http.request(method, path, **kwargs)
        r.raise_for_status()
        return r

    async def _get(self, path, **params):
        return (await self._request("GET", path, params=params)).json()

    async def _all(self, path, **params):
        out = []
        params = {**params, "per_page": 100}
        for _ in range(MAX_PAGES):
            r = await self._request("GET", path, **({"params": params} if params else {}))
            out.extend(r.json())
            link = r.links.get("next", {}).get("url")
            if not link:
                return out
            if urlparse(link).scheme != "https" or urlparse(link).netloc != "api.github.com":
                raise ValueError("GitHub pagination link points outside the API")
            path, params = link, {}
        raise RuntimeError("GitHub pagination budget exceeded; import is incomplete")

    async def get_current_user(self):
        if self.auth:
            return user(await self.auth.identity())
        return user(await self._get("/user"))

    async def get_project(self, project):
        r = await self._get(self._path(project))
        if r.get("private"):
            raise ValueError("the GitHub pilot currently supports public repositories only")
        return {"id": r["id"], "path_with_namespace": r["full_name"],
                "default_branch": r.get("default_branch"), "raw": r}

    async def get_merge_request(self, project, iid):
        r = await self._get(self._path(project, f"/pulls/{iid}"))
        base, head = r["base"]["sha"], r["head"]["sha"]
        compared = await self._get(self._path(project, f"/compare/{base}...{head}"))
        merge_base = compared["merge_base_commit"]["sha"]
        return {"iid": r["number"], "title": r["title"], "description": r.get("body"),
            "state": "merged" if r.get("merged") else "opened" if r["state"] == "open" else "closed",
            "sha": head, "author": user(r["user"]),
            "merged_by": user(r["merged_by"]) if r.get("merged_by") else None,
            "source_branch": r["head"]["ref"], "target_branch": r["base"]["ref"],
            "draft": bool(r.get("draft")), "has_conflicts": r.get("mergeable") is False,
            "labels": [label["name"] for label in r.get("labels", [])],
            "assignees": [user(u) for u in r.get("assignees", [])],
            "web_url": r["html_url"], "created_at": r["created_at"],
            "updated_at": r["updated_at"], "merged_at": r.get("merged_at"),
            "closed_at": r.get("closed_at"), "diff_refs": {"base_sha": merge_base,
                "start_sha": base, "head_sha": head}, "raw": r}

    async def list_merge_requests(self, project, updated_after=None, state="all", selected_iids=None):
        selected = selected_iids if selected_iids is not None else ((self.repo.poll_cursor or {}).get("selected_iids", []) if self.repo else [])
        out = []
        for number in selected:
            r = await self.get_merge_request(project, number)
            if updated_after is None or r["updated_at"] >= updated_after:
                out.append(r)
        return out

    async def list_diffs(self, project, iid):
        rows = await self._all(self._path(project, f"/pulls/{iid}/files"))
        if len(rows) >= 3000:
            raise ValueError("GitHub file listing reached its 3,000-file limit; refusing an incomplete review")
        return [diff(r) for r in rows]

    async def find_review(self, project, iid, marker):
        return next((r for r in await self._reviews(project, iid)
                     if marker in (r.get("body") or "") and r.get("state") != "PENDING"), None)

    async def submit_review(self, project, iid, artifact):
        return (await self._request("POST", self._path(project, f"/pulls/{iid}/reviews"),
            json={"event": "COMMENT", "commit_id": artifact["diff_refs"]["head_sha"],
                  "body": artifact["body"], "comments": [
                      {k: v for k, v in comment.items() if k != "finding_db_id"}
                      for comment in artifact["comments"]]})).json()

    async def list_versions(self, project, iid):
        r = await self.get_merge_request(project, iid)
        refs = r["diff_refs"]
        return [{"id": None, "snapshot_key": f"{r['sha']}:{refs['start_sha']}:{refs['base_sha']}",
                 "head_commit_sha": r["sha"], "base_commit_sha": refs["base_sha"],
                 "start_commit_sha": refs["start_sha"], "created_at": r["updated_at"]}]

    async def _reviews(self, project, iid):
        return await self._all(self._path(project, f"/pulls/{iid}/reviews"))

    async def get_reviewers(self, project, iid):
        reviews = await self._reviews(project, iid)
        latest = {}
        for r in reviews:
            if r["state"] != "PENDING":
                if r["state"] == "COMMENTED" and r["user"]["id"] in latest:
                    continue
                latest[r["user"]["id"]] = {"user": user(r["user"]), "state": r["state"].lower()}
        return list(latest.values())

    async def get_approvals(self, project, iid):
        return {"approved_by": [r for r in await self.get_reviewers(project, iid)
                                if r["state"] == "approved"]}

    async def _thread_metadata(self, project, iid):
        owner, repo = project.split("/")
        query = """query($owner:String!,$repo:String!,$number:Int!,$cursor:String){
          repository(owner:$owner,name:$repo){pullRequest(number:$number){
            reviewThreads(first:100,after:$cursor){pageInfo{hasNextPage endCursor}
              nodes{id isResolved isOutdated viewerCanResolve resolvedBy{login}
                comments(first:1){nodes{databaseId}}}}}}}"""
        cursor, out = None, {}
        for _ in range(MAX_PAGES):
            r = (await self._request("POST", "/graphql", json={"query": query,
                "variables": {"owner": owner, "repo": repo, "number": int(iid), "cursor": cursor}})).json()
            if r.get("errors"):
                raise RuntimeError("GitHub thread query failed; import is incomplete")
            threads = r["data"]["repository"]["pullRequest"]["reviewThreads"]
            for t in threads["nodes"]:
                first = t["comments"]["nodes"]
                if first:
                    out[first[0]["databaseId"]] = t
            if not threads["pageInfo"]["hasNextPage"]:
                return out
            cursor = threads["pageInfo"]["endCursor"]
        raise RuntimeError("GitHub thread pagination budget exceeded")

    async def list_notes(self, project, iid):
        issues = await self._all(self._path(project, f"/issues/{iid}/comments"))
        reviews = await self._reviews(project, iid)
        return [note(r, "issue_comment") for r in issues] + [
            note(r, "review") for r in reviews if r.get("body") and r["state"] != "PENDING"]

    async def list_discussions(self, project, iid):
        comments = await self._all(self._path(project, f"/pulls/{iid}/comments"))
        threads = await self._thread_metadata(project, iid)
        grouped = {}
        for r in comments:
            grouped.setdefault(r.get("in_reply_to_id") or r["id"], []).append(r)
        out = []
        for root, rows in grouped.items():
            t = threads.get(root)
            if t is None:
                raise RuntimeError("GitHub discussion snapshot changed; refresh before reviewing")
            self._thread_roots[t["id"]] = root
            notes = [note(r) for r in sorted(rows, key=lambda r: (r["created_at"], r["id"]))]
            for n in notes:
                n.update(resolvable=True, resolved=t["isResolved"])
                resolver = t.get("resolvedBy")
                if resolver:
                    n["resolved_by"] = user(await self._get(f"/users/{quote(resolver['login'], safe='')}"))
            out.append({"id": t["id"], "notes": notes, "raw": t})
        for n in await self.list_notes(project, iid):
            out.append({"id": n["provider_note_key"], "individual_note": True, "notes": [n]})
        return out

    async def get_note_awards(self, project, iid, note_id, *, note_key=None):
        kind = (note_key or "review_comment:").split(":", 1)[0]
        if kind == "review":
            return []  # GitHub has no reaction endpoint for a submitted review body.
        path = f"/issues/comments/{note_id}/reactions" if kind == "issue_comment" else f"/pulls/comments/{note_id}/reactions"
        names = {"+1": "thumbsup", "-1": "thumbsdown"}
        return [{"id": r["id"], "name": names.get(r["content"], r["content"]),
                 "user": user(r["user"])} for r in await self._all(self._path(project, path))]

    async def get_branch(self, project, branch):
        try:
            r = await self._request("GET", self._path(project, f"/branches/{quote(branch, safe='')}"))
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise
        data = r.json()
        return {"name": data["name"], "commit": {"id": data["commit"]["sha"]}}

    async def compare(self, project, frm, to):
        r = await self._get(self._path(project, f"/compare/{quote(frm, safe='')}...{quote(to, safe='')}"))
        if len(r.get("files", [])) >= 300:
            raise ValueError("GitHub comparison reached its file limit; use a full review")
        return {"diffs": [diff(f) for f in r.get("files", [])],
                "commits": [{"id": c["sha"], "title": c["commit"]["message"]} for c in r.get("commits", [])]}

    async def get_file_raw(self, project, path, ref):
        r = await self._get(self._path(project, f"/contents/{quote(path, safe='/')}"), ref=ref)
        if not isinstance(r, dict) or r.get("encoding") != "base64":
            raise ValueError("GitHub file contents are unavailable")
        return base64.b64decode(r["content"]).decode("utf-8")

    async def create_discussion(self, project, iid, body, position=None):
        if position:
            line = position.get("new_line")
            payload = {"body": body, "commit_id": position["head_sha"],
                       "path": position["new_path"], "line": line, "side": "RIGHT"}
            if line is None:
                raise ValueError("GitHub inline comments need a new-file line")
            path, kind = f"/pulls/{iid}/comments", "review_comment"
        else:
            payload, path, kind = {"body": body}, f"/issues/{iid}/comments", "issue_comment"
        r = (await self._request("POST", self._path(project, path), json=payload)).json()
        n = note(r, kind)
        return {"id": r.get("node_id") or n["provider_note_key"], "notes": [n]}

    async def create_note(self, project, iid, discussion_id, body):
        root = self._thread_roots.get(discussion_id)
        if root is None:
            await self.list_discussions(project, iid)
            root = self._thread_roots.get(discussion_id)
        if root is None:
            raise ValueError("GitHub reply needs an existing review thread")
        r = (await self._request("POST", self._path(project,
            f"/pulls/{iid}/comments/{root}/replies"), json={"body": body})).json()
        return note(r)

    async def resolve_discussion(self, project, iid, discussion_id, resolved):
        operation = "resolveReviewThread" if resolved else "unresolveReviewThread"
        query = f"mutation($id:ID!){{{operation}(input:{{threadId:$id}}){{thread{{id isResolved}}}}}}"
        r = (await self._request("POST", "/graphql", json={"query": query,
            "variables": {"id": discussion_id}})).json()
        if r.get("errors"):
            raise RuntimeError("GitHub thread resolution failed")
        return r["data"][operation]["thread"]
