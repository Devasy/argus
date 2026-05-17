"""Repository call-graph impact analysis via the optional external `graphify`
CLI. When the binary isn't on PATH, review continues without this tool."""
import asyncio
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("argus.graphify")

_HUNK_HEADER_RE = re.compile(r"^@@[^@]*@@\s*(.*)")
_HUNK_CONTEXT_PATTERNS = [
    re.compile(r"(?:async\s+)?def\s+(\w+)\s*\("),
    re.compile(r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[\(<]"),
    re.compile(r"(?:public|private|protected|static|\s)+\w+\s+(\w+)\s*\("),
]
_ADDED_DEF_PATTERNS = [
    re.compile(r"^\+\s*(?:async\s+)?def\s+(\w+)\s*\("),
    re.compile(r"^\+\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[\(<]"),
]


def extract_changed_functions(diff_text: str) -> list[str]:
    """Return names of functions that are new or whose body was touched in
    this unified diff, in first-seen order with duplicates removed."""
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for line in diff_text.splitlines():
        hm = _HUNK_HEADER_RE.match(line)
        if hm:
            context = hm.group(1).strip()
            for pat in _HUNK_CONTEXT_PATTERNS:
                cm = pat.search(context)
                if cm:
                    _add(cm.group(1))
                    break
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pat in _ADDED_DEF_PATTERNS:
            m = pat.match(line)
            if m:
                _add(m.group(1))
                break

    return names


_GRAPHIFYIGNORE_DEFAULT = "lib/*\n"

# `graphify extract` refuses to run when the corpus contains docs, papers or
# images: those need an LLM key for semantic extraction, and it aborts even
# when every other file is code and needs none. We only ever use the code call
# graph, so we exclude non-code and the corpus becomes code-only.
#
# This must be passed as `--exclude`, NOT written into .graphifyignore: the
# ignore file is applied during graph construction, but the doc/paper/image
# detection that triggers the key check happens before it and still counts
# those files. --exclude is anchored at the scan root and applied to detection
# itself.
#
# The stakes are higher than a skipped feature. When extract aborts we fall
# back to `graphify update --force`, which builds NODES BUT NO EDGES -- and a
# call graph with no edges answers "callers=[none] callees=[none]" for every
# symbol, which is indistinguishable from a genuine "nothing calls this".
# Every graph in production was built by that fallback, so blast-radius
# analysis has been silently vacuous since the feature shipped, and agents
# correctly concluded the graph tools were not worth calling.
# graphify classifies by content as well as extension, so this list is wider
# than "documentation": on a real React repo the last three offenders were
# .gitlab-ci.yml, public/index.html and public/robots.txt. Any single
# unexcluded file aborts the whole extract, so err toward excluding anything
# that is not source we want call edges from.
_NON_CODE_EXCLUDES = [
    # prose / docs
    "*.md", "*.mdx", "*.rst", "*.txt", "*.pdf", "*.docx", "*.csv",
    # images / media
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.webp", "*.ico",
    "*.mp4", "*.mp3", "*.wav", "*.mov",
    # markup and config that graphify treats as documents
    "*.html", "*.htm", "*.xml", "*.yml", "*.yaml", "*.toml", "*.ini",
    "*.cfg", "*.lock", "robots.txt", "LICENSE", "CHANGELOG",
]


def _exclude_args() -> list[str]:
    args: list[str] = []
    for pattern in _NON_CODE_EXCLUDES:
        args += ["--exclude", pattern]
    return args


def _write_graphifyignore(workspace: Path) -> None:
    ignore_path = workspace / ".graphifyignore"
    if not ignore_path.exists():
        ignore_path.write_text(_GRAPHIFYIGNORE_DEFAULT)


async def build_graph(workspace: Path) -> Path | None:
    if shutil.which("graphify") is None:
        logger.warning("graphify binary not on PATH; skipping graph build")
        return None

    def _sync() -> Path | None:
        _write_graphifyignore(workspace)
        out = workspace / "graphify-out" / "graph.json"
        try:
            subprocess.run(["graphify", "extract", ".", "--no-cluster",
                            *_exclude_args()],
                           cwd=workspace, capture_output=True, timeout=600,
                           check=True)
        except Exception as e:
            logger.warning("graphify extract failed: %s", e)
            if out.exists():
                return out
            # extract can fail when non-code files (docs, images) need an
            # LLM key for classification; `update --force` is code-only and
            # never needs one.
            logger.info("falling back to graphify update --force (code-only)")
            try:
                subprocess.run(["graphify", "update", ".", "--force"],
                               cwd=workspace, capture_output=True,
                               timeout=600, check=True)
            except Exception as e2:
                logger.warning("graphify update --force failed: %s", e2)
                return None
        if out.exists():
            # Loudly surface an edgeless graph. `update --force` produces
            # nodes but no edges, and a call graph without edges silently
            # answers "no callers, no callees" for everything -- which reads
            # as a true negative rather than a missing graph. This went
            # unnoticed in production for the life of the feature.
            try:
                data = json.loads(out.read_text())
                n_edges = len(data.get("edges") or [])
                if n_edges == 0:
                    logger.warning(
                        "graph at %s has %d nodes but ZERO edges -- call-graph "
                        "tools cannot report blast radius and will answer "
                        "'callers=[none] callees=[none]' for every symbol",
                        out, len(data.get("nodes") or []))
                else:
                    logger.info("graph built: %d nodes, %d edges",
                                len(data.get("nodes") or []), n_edges)
            except Exception:
                logger.warning("could not inspect built graph at %s", out)
        return out if out.exists() else None

    return await asyncio.to_thread(_sync)


def load_graph(graph_path: Path) -> dict:
    return json.loads(graph_path.read_text())


_LABEL_NAME_RE = re.compile(r"^(\w+)")


def _node_file(n: dict) -> str | None:
    """Return a node's source file path, supporting both the hand-built test
    fixture schema (`file`) and the real `graphify` CLI schema (`source_file`)."""
    return n.get("file") or n.get("source_file")


def _node_name(n: dict) -> str | None:
    """Return a node's bare function/symbol name, supporting both the
    hand-built test fixture schema (`name`) and the real `graphify` CLI
    schema, where `label` looks like `"check_token()"`."""
    name = n.get("name")
    if name:
        return name
    label = n.get("label")
    if not label:
        return None
    m = _LABEL_NAME_RE.match(label)
    return m.group(1) if m else label


def impacted_by(graph: dict, changed_paths: set[str], depth: int = 2) -> list[str]:
    nodes = {n["id"]: n for n in graph.get("nodes") or []}
    reverse: dict[str, list[str]] = {}
    for e in graph.get("edges") or []:
        reverse.setdefault(e["target"], []).append(e["source"])
    frontier = {nid for nid, n in nodes.items() if _node_file(n) in changed_paths}
    seen = set(frontier)
    for _ in range(depth):
        frontier = {src for nid in frontier for src in reverse.get(nid, [])
                    if src not in seen}
        seen |= frontier
    out = []
    for nid in seen:
        n = nodes.get(nid)
        if n:
            out.append(f"{_node_file(n)} :: {_node_name(n)}")
    return sorted(set(out))[:50]


def diff_impact(graph: dict, changed_functions: list[tuple[str, str]],
                direction: str = "both", depth: int = 2) -> list[dict]:
    """For each (function_name, file_path) changed in the diff, return its
    callers and/or callees from the call graph, up to `depth` hops."""
    nodes = {n["id"]: n for n in graph.get("nodes") or []}
    by_file_name: dict[tuple[str, str], str] = {
        (_node_file(n), _node_name(n)): nid for nid, n in nodes.items()
    }
    forward: dict[str, list[tuple[str, str]]] = {}
    reverse: dict[str, list[tuple[str, str]]] = {}
    for e in graph.get("edges") or []:
        rel = e.get("relation", "")
        forward.setdefault(e["source"], []).append((e["target"], rel))
        reverse.setdefault(e["target"], []).append((e["source"], rel))

    def _walk(seed: str, edge_map: dict[str, list[tuple[str, str]]]) -> list[dict]:
        out = []
        frontier = {seed}
        seen = {seed}
        for hop in range(depth):
            next_frontier = set()
            for nid in frontier:
                for neighbor, rel in edge_map.get(nid, []):
                    if neighbor in seen:
                        continue
                    seen.add(neighbor)
                    next_frontier.add(neighbor)
                    n = nodes.get(neighbor)
                    if n:
                        out.append({"function": _node_name(n), "file": _node_file(n),
                                   "relation": rel, "depth": hop + 1})
            frontier = next_frontier
            if not frontier:
                break
        return out

    results = []
    for fn_name, file_path in changed_functions:
        seed = by_file_name.get((file_path, fn_name))
        callers: list[dict] = []
        callees: list[dict] = []
        if seed is not None:
            if direction in ("callers", "both"):
                callers = _walk(seed, reverse)
            if direction in ("callees", "both"):
                callees = _walk(seed, forward)
        results.append({
            "changed_function": fn_name,
            "file": file_path,
            "seed_found": seed is not None,
            "callers": callers,
            "callees": callees,
        })
    return results


def build_graphify_tool(graph_path: Path, changed_paths: set[str],
                        hunks: "dict[str, object] | None" = None,
                        files_by_id: "dict[str, object] | None" = None) -> list:
    from langchain_core.tools import tool

    @tool
    async def graph_impact() -> str:
        """List functions/classes transitively affected by this MR's changed
        files, from the repository call graph. Works from the changed FILE
        list, so unlike graph_diff_impact it still works when GitLab collapsed
        the diff and no hunk text is available."""
        try:
            graph = load_graph(graph_path)
        except Exception as e:
            return (f"graph unavailable: {e}. Fall back to "
                    "search_code(pattern='<symbol>') to find callers by text.")
        items = impacted_by(graph, changed_paths)
        if not items:
            return ("no impact edges found -- the call graph has no edges into "
                    "or out of the changed files. Common for a first commit "
                    "in a new module, config/markup changes, or a language the "
                    "graph does not index. To find callers anyway, use "
                    "search_code(pattern='<function name>'). Do not repeat "
                    "this call.")
        return "\n".join(items)

    tools = [graph_impact]

    # Registered whenever the caller supplied a diff context at all, even an
    # EMPTY hunk map. Gating on `hunks` being non-empty meant that on a
    # collapsed MR -- the case where blast radius matters most -- the tool
    # silently did not exist, so the agent could not be told why or pointed at
    # graph_impact instead. An unhelpful answer is recoverable; an absent tool
    # is not.
    if files_by_id is not None:
        @tool
        async def graph_diff_impact(direction: str = "both", depth: int = 2) -> str:
            """Automatically find all functions added or modified in this MR's
            diff and return their callers and callees from the repository call
            graph. Call this early to understand the blast radius of the
            changes: which existing code calls into the changed functions,
            and what those functions depend on. direction is one of
            'callers', 'callees', or 'both' (default). depth is hop count
            (default 2)."""
            try:
                graph = load_graph(graph_path)
            except Exception as e:
                return f"graph unavailable: {e}"
            changed_functions: list[tuple[str, str]] = []
            for h in (hunks or {}).values():
                fc = files_by_id.get(h.file_id)
                if fc is None:
                    continue
                for fn_name in extract_changed_functions(h.diff_text):
                    changed_functions.append((fn_name, fc.path))
            if not changed_functions:
                # Distinguish "the diff genuinely defines no functions" from
                # "there was no diff text to read". On a large MR GitLab
                # collapses file diffs, so `hunks` is empty and this tool used
                # to answer a flat "no function definitions found in the diff"
                # -- which reads as "the graph is useless here" and is why
                # agents abandoned it on exactly the MRs where blast-radius
                # information matters most. Point at graph_impact instead,
                # which works from the changed *file* list and needs no hunks.
                collapsed = [f for f in files_by_id.values()
                             if not f.diff_available]
                if collapsed and not hunks:
                    return ("no diff text available -- GitLab collapsed this "
                            f"MR's file diffs ({len(collapsed)} file(s)). This "
                            "tool needs hunk text to find changed functions. "
                            "Use graph_impact instead: it derives impact from "
                            "the changed file list and does not need hunks.")
                return ("no function definitions found in the diff -- the "
                        "changes are inside existing function bodies, or in "
                        "files the graph does not index (config, markup, "
                        "styles). Use graph_impact for file-level blast "
                        "radius, or search_code to find callers by name.")
            results = diff_impact(graph, changed_functions, direction=direction, depth=depth)
            lines = []
            missing: list[str] = []
            for r in results:
                if not r["seed_found"]:
                    missing.append(r["changed_function"])
                    continue
                callers = ", ".join(c["function"] for c in r["callers"]) or "none"
                callees = ", ".join(c["function"] for c in r["callees"]) or "none"
                lines.append(f"{r['changed_function']} ({r['file']}): "
                             f"callers=[{callers}] callees=[{callees}]")
            # Say how much of the diff the graph actually covered. Without
            # this a partial answer looks like a complete one, and an agent
            # cannot tell "this function has no callers" from "the graph does
            # not know about this function" -- the difference between a safe
            # change and an unaudited one.
            found = len(lines)
            total = len(results)
            header = (f"call graph covered {found}/{total} changed symbol(s) "
                      f"at depth {depth}")
            # A graph with no edges resolves every seed and reports every one
            # as having no callers and no callees. That is indistinguishable
            # from a true "nothing calls this" unless we say so, and reading
            # it as a true negative is how a breaking change gets approved.
            if found and not any("callers=[none] callees=[none]" not in ln
                                 for ln in lines):
                return (f"{header}\nWARNING: the call graph contains NO EDGES, "
                        "so every symbol reports no callers and no callees. "
                        "This is a missing graph, NOT evidence that the "
                        "changed code is unused. Treat blast radius as UNKNOWN "
                        "and use search_code(pattern='<name>') to find callers "
                        "by text. Do not repeat this call.")
            if missing:
                shown = ", ".join(missing[:15])
                header += (f"; NOT in graph: {shown}"
                           f"{', ...' if len(missing) > 15 else ''} -- for "
                           "these the graph proves nothing, so use "
                           "search_code(pattern='<name>') before concluding "
                           "a symbol is unused")
            if not lines:
                return (header + "\n(no call-graph edges resolved for this "
                        "diff; do not repeat this call unchanged)")
            return header + "\n" + "\n".join(lines)

        tools.append(graph_diff_impact)

    return tools
