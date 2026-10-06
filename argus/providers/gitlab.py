"""Compatibility adapter around the established GitLab transport."""
from urllib.parse import quote

from argus.providers.base import Capabilities


class GitLabProvider:
    provider_name = "gitlab"
    capabilities = Capabilities()

    def __init__(self, settings, repo=None):
        from argus.gitlab.client import GitLabClient
        self.settings = settings
        self.client = GitLabClient(settings.gitlab_url, settings.gitlab_token,
                                   settings.gitlab_ca_bundle or settings.gitlab_ssl_verify)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def clone_url(self, project_path: str) -> str:
        token = quote(self.settings.gitlab_token, safe="")
        base = self.settings.gitlab_url.rstrip("/").replace("://", f"://oauth2:{token}@", 1)
        return f"{base}/{project_path}.git"

    def review_ref(self, number: int) -> str:
        return f"refs/merge-requests/{number}/head"

    async def get_note_awards(self, project, iid, note_id, *, note_key=None):
        return await self.client.get_note_awards(project, iid, note_id)
