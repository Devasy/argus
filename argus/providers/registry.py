"""One registration point for hosting integrations."""
from pathlib import Path

from argus.providers.gitlab import GitLabProvider
from argus.providers.github import GitHubProvider

PROVIDERS = {"gitlab": GitLabProvider, "github": GitHubProvider}


def create_provider(settings, repo=None, *, provider=None):
    name = provider or (repo.provider if repo is not None else "gitlab")
    try:
        factory = PROVIDERS[name]
    except KeyError:
        raise ValueError(f"unsupported repository provider: {name}") from None
    return factory(settings, repo)


def repository_key(repo):
    # Legacy callers and stored rows retain their numeric GitLab project ID.
    return repo.project_path if getattr(repo, "provider", "gitlab") != "gitlab" else repo.gitlab_project_id


def workspace_for(settings, repo, provider):
    from argus.review.workspace import WorkspaceManager
    return WorkspaceManager(Path(settings.workspace_root).expanduser() / str(repo.id),
                            provider.clone_url(repo.project_path),
                            review_ref=provider.review_ref)
