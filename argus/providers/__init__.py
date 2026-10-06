"""Git hosting adapters. Core jobs select an adapter, never a host's SDK."""
from argus.providers.registry import create_provider, repository_key, workspace_for

__all__ = ["create_provider", "repository_key", "workspace_for"]
