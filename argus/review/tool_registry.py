"""Single source of truth for "what tools can a reviewer agent be given",
served to the frontend so its tool-allowlist picker never drifts from the
tools the pipeline actually registers.

Before this, frontend/src/features/agents/toolAllowlist.ts hardcoded a list
of tool names by hand -- and it went stale the moment outline_file/search_code
were added to build_read_tools: they had no chip in the allowlist editor and
were invisible in the UI, even though the pipeline granted them anyway (they
are in ALWAYS_AVAILABLE_TOOLS). A hardcoded list is exactly the kind of drift
this system already burned an afternoon on with review_stages tool_allowlist
rows predating these tools; the frontend must not repeat it structurally.

Every @tool-decorated function's name and description are fixed at
definition time -- they do not depend on the runtime arguments a factory
closure captures (a workspace path, a DB session factory, discovered
skills). So each factory below is called once with cheap placeholder
arguments purely to read `.name`/`.description` off the resulting
StructuredTool objects; the closures are never invoked. This module is
import-light and touches no database or filesystem.
"""
from pathlib import Path

from argus.review.tools import (ToolContext, build_file_knowledge_tool,
                                    build_learning_tool,
                                    build_learnings_search_tool,
                                    build_learnings_upsert_tool,
                                    build_read_tools,
                                    build_report_problem_tool,
                                    build_skill_tools)


def _placeholder_ctx() -> ToolContext:
    return ToolContext(workspace=Path("."), files_by_id={}, hunks={})


def registered_tools() -> list[dict]:
    """Every tool name + description the pipeline can register, deduplicated
    and sorted by name. `always_available` flags the ones exempt from
    tool_allowlist filtering (argus.review.pipeline.ALWAYS_AVAILABLE_TOOLS)
    -- the frontend should render those as informational, not as a toggle,
    since unchecking one would not actually revoke access."""
    from argus.review.pipeline import ALWAYS_AVAILABLE_TOOLS

    tools = []
    tools += build_read_tools(_placeholder_ctx())
    tools += build_skill_tools(Path("."), skills=[])
    tools.append(build_learning_tool(sf=None))
    tools.append(build_learnings_search_tool(sf=None, settings=None, repo_id=None))
    tools.append(build_learnings_upsert_tool(sf=None, settings=None, repo_id=None))
    tools.append(build_file_knowledge_tool(sf=None, settings=None, repo_id=None,
                                           workspace=Path(".")))
    # Bound per stage in the pipeline rather than once into base_tools, so it
    # needs an explicit placeholder here to appear in the picker at all.
    tools.append(build_report_problem_tool(sf=None, review_id=None,
                                           stage_name="scout"))
    # graph_diff_impact is registered only when a diff context is supplied
    # (see graphify.build_graphify_tool); pass one so it shows up here too --
    # it is a real, sometimes-available tool, not a hypothetical one.
    from argus.review.artifacts import FileChange
    from argus.knowledge.graphify import build_graphify_tool
    tools += build_graphify_tool(Path("graph.json"), set(),
                                 hunks={}, files_by_id={
                                     "f1": FileChange(file_id="f1", path="x",
                                                      change_kind="modified")})

    import inspect

    seen: dict[str, dict] = {}
    for t in tools:
        # Docstrings come through with their source indentation and internal
        # newlines (LangChain uses them verbatim as the tool description for
        # the model, where whitespace is harmless). inspect.cleandoc collapses
        # that to normalized text for a UI list, where raw indentation would
        # render as a wall of leading spaces on every wrapped line.
        seen[t.name] = {
            "name": t.name,
            "description": inspect.cleandoc(t.description or ""),
            "always_available": t.name in ALWAYS_AVAILABLE_TOOLS,
        }
    return sorted(seen.values(), key=lambda d: d["name"])
