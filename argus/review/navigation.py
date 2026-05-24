"""Structural navigation over the review worktree: outline a file's symbols,
or search the repository for a pattern.

Both answer questions agents previously answered by brute force. Across the 66
recursion-limit failures the dominant tool call was get_file_lines (1449
calls, vs 85 for the call-graph tool) -- agents paging through files 100 lines
at a time to work out what was in them, and to find where a symbol lived.
An outline answers "what is in this file, and at which line" for a few hundred
tokens instead of several thousand, and a search answers "where is this
defined" without reading anything.

Deliberately dependency-free: Python files are parsed with the stdlib `ast`
(exact), everything else with the same regex approach the graphify module
already uses for diff headers, and search shells out to ripgrep only when it
is present, falling back to a pure-Python walk. The runtime image today has
neither ripgrep nor a full tree-sitter grammar set, and a review must never
fail because a navigation aid is unavailable.
"""
import ast
import asyncio
import fnmatch
import re
import shutil
import subprocess
from pathlib import Path

# Directories that are never worth searching or outlining. Walking .git on a
# large repo costs seconds and returns nothing a reviewer wants.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
             "build", ".mypy_cache", ".pytest_cache", ".next", "target",
             "vendor", ".tox", "coverage"}
MAX_SEARCH_BYTES = 2_000_000   # skip files larger than this when searching
SEARCH_TIMEOUT_S = 20

# Non-Python symbol declarations. Ordered most- to least-specific; the first
# match wins so `export class Foo` reports as a class, not a bare export.
_SYMBOL_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Python def/class come first so a .py file that failed to parse -- which
    # is common for a file mid-edit in a diff -- still yields a usable
    # outline instead of falling through to "no symbols detected".
    ("def", re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")),
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)")),
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)")),
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*=")),
    ("enum", re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+(\w+)")),
    ("func", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s+(\w+)")),
    ("func", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)")),          # go
    ("const", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*="
                         r"\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>")),     # arrow fn
    ("method", re.compile(r"^\s*(?:public|private|protected|static|final|\s)+"
                          r"[\w<>\[\],\s]+\s+(\w+)\s*\([^)]*\)\s*\{")),   # java/c#
]
_IMPORT_RE = re.compile(r"^\s*(?:import\s|from\s+[\w.]+\s+import\s|#include\s|"
                        r"(?:const|let|var)\s+\w+\s*=\s*require\()")


def _rel(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def resolve_in_workspace(workspace: Path, path: str) -> Path | None:
    """Resolve `path` under the worktree, or None if it escapes.

    The agent supplies this string, and on a repo it has only partially read
    it will sometimes supply nonsense; containment is checked here rather than
    trusted at the call site."""
    root = workspace.resolve()
    target = (root / path).resolve()
    return target if target.is_relative_to(root) else None


def _python_outline(source: str) -> list[str] | None:
    """Exact outline via the stdlib parser. Returns None if the file does not
    parse, so the caller can fall back to regex rather than report nothing --
    a file mid-edit in a diff is often syntactically broken."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    lines: list[str] = []

    def sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        args = [a.arg for a in node.args.args]
        if node.args.vararg:
            args.append("*" + node.args.vararg.arg)
        if node.args.kwarg:
            args.append("**" + node.args.kwarg.arg)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)})"

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(f"{node.lineno:>5}  {sig(node)}")
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            lines.append(f"{node.lineno:>5}  class {node.name}"
                         f"{f'({bases})' if bases else ''}")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append(f"{sub.lineno:>5}      {sig(sub)}")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    lines.append(f"{node.lineno:>5}  {t.id} = ...")
    return lines


def _regex_outline(source: str) -> list[str]:
    """Best-effort outline for everything that is not Python."""
    out: list[str] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if len(line) > 400:
            continue  # minified bundle; nothing useful to extract
        for kind, pat in _SYMBOL_PATTERNS:
            m = pat.match(line)
            if m:
                out.append(f"{i:>5}  {kind} {m.group(1)}")
                break
    return out


def outline(source: str, path: str) -> str:
    """Symbol outline for one file's text."""
    total = source.count("\n") + 1
    symbols = _python_outline(source) if path.endswith(".py") else None
    how = "ast"
    if symbols is None:
        symbols = _regex_outline(source)
        how = "regex"
    imports = sum(1 for ln in source.splitlines() if _IMPORT_RE.match(ln))
    if not symbols:
        return (f"{path}: {total} lines, no top-level symbols detected "
                f"({how}). It may be data, config, or markup -- read it with "
                f"get_file_lines if you need its contents.")
    head = (f"{path}: {total} lines, {len(symbols)} symbols, ~{imports} "
            f"imports (outline via {how}; line numbers are exact)")
    return head + "\n" + "\n".join(symbols)


def iter_source_files(workspace: Path):
    """Every candidate file under the worktree, skipping vendor/build noise."""
    for p in workspace.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def _search_ripgrep(workspace: Path, pattern: str, glob: str,
                    context: int, max_matches: int) -> list[str] | None:
    """Use ripgrep when the image has it. Returns None when it is absent or
    fails, so the caller falls back rather than surfacing an error."""
    rg = shutil.which("rg")
    if rg is None:
        return None
    cmd = [rg, "--line-number", "--no-heading", "--color=never",
           "--max-count", str(max_matches), "-C", str(context)]
    for d in SKIP_DIRS:
        cmd += ["--glob", f"!{d}/**"]
    if glob:
        cmd += ["--glob", glob]
    cmd += ["--regexp", pattern, "."]
    try:
        proc = subprocess.run(cmd, cwd=workspace, capture_output=True,
                              text=True, timeout=SEARCH_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode not in (0, 1):     # 1 == no matches, which is a result
        return None
    return proc.stdout.splitlines()


def _search_python(workspace: Path, pattern: str, glob: str,
                   context: int, max_matches: int) -> list[str]:
    """Pure-Python fallback. Slower than ripgrep but always available, and the
    worktrees involved are single repositories rather than a whole disk."""
    rx = re.compile(pattern)
    out: list[str] = []
    for p in iter_source_files(workspace):
        rel = _rel(p, workspace)
        # Path.match() only matches a fixed number of trailing path segments
        # and treats "**" as a literal single-segment wildcard, so recursive
        # patterns like "acme/**/*.py" never match a nested file. fnmatch
        # already lets "*" cross "/" boundaries, giving "**" the recursive
        # behavior callers expect.
        if glob and not fnmatch.fnmatch(rel, glob):
            continue
        try:
            if p.stat().st_size > MAX_SEARCH_BYTES:
                continue
            lines = p.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if not rx.search(line):
                continue
            lo = max(0, i - context)
            hi = min(len(lines), i + context + 1)
            for j in range(lo, hi):
                sep = ":" if j == i else "-"
                out.append(f"{rel}{sep}{j + 1}{sep}{lines[j][:400]}")
            if len(out) >= max_matches * (2 * context + 1):
                return out
    return out


async def search(workspace: Path, pattern: str, glob: str = "",
                 context: int = 2, max_matches: int = 50) -> list[str]:
    """Search the worktree, preferring ripgrep and degrading to Python."""
    def _sync() -> list[str]:
        hits = _search_ripgrep(workspace, pattern, glob, context, max_matches)
        if hits is None:
            hits = _search_python(workspace, pattern, glob, context, max_matches)
        return hits
    return await asyncio.to_thread(_sync)
