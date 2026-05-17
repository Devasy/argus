"""Query text for learning retrieval.

Three builders, deliberately separate so each can be scored on its own:

* `learnings_query_text` -- what production has always sent: title, paths and
  the first 2000 chars of added lines.
* `identifier_query` -- the rare identifiers from the *whole* diff, rarest
  first. For a large MR the 2000-char budget above covers a sliver of the
  change, and identifiers are what a lexical arm can actually match on.
* `scout_query` -- scout's own summaries, which are already dense with the
  identifiers a diff introduces and cost nothing extra to reuse.
"""
import re
from collections import Counter

MAX_QUERY_TERMS = 64
DIFF_SIGNAL_CHARS = 2000
MIN_PLAIN_WORD_LEN = 4

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HAS_INNER_UPPER = re.compile(r"[a-z0-9][A-Z]")

# Keywords and near-universal identifiers carry no retrieval signal; they are
# dropped by name rather than by frequency because a single diff is too small
# a sample for their commonness to show up as a high document frequency.
_STOPWORDS = frozenset("""
and as assert async await break by case catch class const constructor continue
def default del delete do elif else end enum except export extends false final
finally for from func function global goto if implements import in instanceof
interface is lambda let match new next none nonlocal not null of or pass print
private protected public raise return self static super switch template then
this throw throws true try type typedef typeof union unless until var void
when where while with yield
""".split())


def _candidates(text: str) -> list[str]:
    out = []
    for tok in _TOKEN.findall(text or ""):
        if tok.lower() in _STOPWORDS:
            continue
        if "_" in tok:
            if any(c.isalpha() for c in tok):
                out.append(tok)
            continue
        if _HAS_INNER_UPPER.search(tok) or tok.isupper():
            out.append(tok)
            continue
        if len(tok) >= MIN_PLAIN_WORD_LEN:
            out.append(tok)
    return out


def identifier_query(added_lines: list[str],
                     max_terms: int = MAX_QUERY_TERMS) -> str:
    """Rarest-first, deduplicated identifier terms over every added line. No
    character budget: the whole diff is read, then the term count is capped."""
    counts: Counter = Counter()
    order: dict[str, int] = {}
    for line in added_lines or []:
        for tok in _candidates(line):
            counts[tok] += 1
            order.setdefault(tok, len(order))
    ranked = sorted(counts, key=lambda t: (counts[t], order[t]))
    return " ".join(ranked[:max_terms])


def scout_query(mr_summary: str | None,
                file_summaries: dict[str, str | None],
                changed_paths: list[str]) -> str:
    parts: list[str] = []
    if mr_summary:
        parts.append(mr_summary)
    for path, summary in (file_summaries or {}).items():
        if summary:
            parts.append(f"{path}: {summary}")
    if changed_paths:
        parts.append(" ".join(changed_paths))
    return "\n".join(parts)


def learnings_query_text(mr_title: str, changed_paths: list[str],
                         added_lines: list[str],
                         max_chars: int = DIFF_SIGNAL_CHARS,
                         max_path_chars: int = DIFF_SIGNAL_CHARS) -> str:
    """The historical production query, kept verbatim so the benchmark has a
    baseline it can actually reproduce -- paths capped the same way
    runner.py::_learnings_query_text is, since an uncapped path list is what
    actually crashed a production review (see runner.py's docstring there)."""
    signal = " ".join(added_lines or [])[:max_chars]
    paths = " ".join(changed_paths)[:max_path_chars]
    return f"{mr_title} {paths} {signal}".strip()
