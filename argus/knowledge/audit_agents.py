"""Contracts, prompts and coverage rules for the audit pipeline's agents (no LLM calls here)."""
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from argus.knowledge.auditor import Citation

INVESTIGATE_MAX_ROUNDS = 8


@dataclass(frozen=True)
class LearningCard:
    id: str
    topic: str
    hint_text: str
    kind: str
    file_paths: tuple[str, ...]
    file_pattern: str | None
    evidence: str


def card_for(l) -> LearningCard:
    total = l.hit_count + l.harmful_count + l.ignored_count + l.miss_count + l.inconclusive_count
    evidence = (f"injected {total}x: {l.hit_count} accepted, {l.harmful_count} rejected, "
                f"{l.ignored_count} unused" if total else "never injected yet")
    return LearningCard(str(l.id), l.topic, l.hint_text, l.kind,
                        tuple(p for p in (l.file_paths or []) if isinstance(p, str)),
                        l.file_pattern, evidence)


def format_cards(cards, notes: dict[str, str] | None = None) -> str:
    notes = notes or {}
    out = []
    for c in cards:
        out.append(f"--- learning_id={c.id} ({c.kind}) ---\ntopic: {c.topic}\nhint: {c.hint_text}\n"
                   f"claimed file_paths: {list(c.file_paths)} · file_pattern: {c.file_pattern or '(none)'}\n"
                   f"outcome evidence: {c.evidence}"
                   + (f"\nscout note: {notes[c.id]}" if c.id in notes else ""))
    return "\n\n".join(out)


class ScoutGroup(BaseModel):
    group_id: str
    learning_ids: list[str]
    area: str = ""


class AuditScoutPlan(BaseModel):
    groups: list[ScoutGroup] = []
    notes: dict[str, str] = {}


class GroundVerdict(BaseModel):
    learning_id: str
    verdict: Literal["corroborated", "stale", "contradicted", "unfalsifiable", "ungrounded"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    citations: list[Citation] = []
    suggested_hint_text: str | None = None


class GroundList(BaseModel):
    verdicts: list[GroundVerdict] = []


class Relation(BaseModel):
    learning_id: str
    relation: Literal["duplicate_of", "conflicts_with"]
    related_learning_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    suggested_hint_text: str | None = None


class RelationList(BaseModel):
    relations: list[Relation] = []


class ProposalCheck(BaseModel):
    learning_id: str
    confirmed: bool
    reason: str


class CheckList(BaseModel):
    checks: list[ProposalCheck] = []


class GroupSpec(BaseModel):
    group_id: str
    learning_ids: list[str]
    area: str = ""


def _norm(v) -> str | None:
    try:
        return str(uuid.UUID(str(v).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def plan_groups(plan: AuditScoutPlan | None, due_ids: list[str], max_group_size: int) -> list[GroupSpec]:
    """Every due learning in exactly one group of at most max_group_size, whatever the scout returned."""
    due = {_norm(i): i for i in due_ids}
    seen: set[str] = set()
    raw: list[tuple[list[str], str]] = []
    for g in (plan.groups if plan else []):
        ids = []
        for i in g.learning_ids:
            n = _norm(i)
            if n in due and n not in seen:
                seen.add(n)
                ids.append(due[n])
        for k in range(0, len(ids), max_group_size):
            raw.append((ids[k:k + max_group_size], g.area))
    rest = [i for n, i in due.items() if n not in seen]
    for k in range(0, len(rest), max_group_size):
        raw.append((rest[k:k + max_group_size], "(not placed by scout)"))
    return [GroupSpec(group_id=f"g{n}", learning_ids=ids, area=area)
            for n, (ids, area) in enumerate((r for r in raw if r[0]), start=1)]


def keep_known(verdicts, ids: set[str]) -> list:
    wanted = {_norm(i): i for i in ids}
    out, seen = [], set()
    for v in verdicts:
        n = _norm(v.learning_id)
        if n in wanted and n not in seen:
            seen.add(n)
            out.append(v.model_copy(update={"learning_id": wanted[n]}))
    return out


def valid_relations(relations, cluster_ids: set[str]) -> list:
    ids = {_norm(i): i for i in cluster_ids}
    out = []
    for r in relations:
        a, b = _norm(r.learning_id), _norm(r.related_learning_id)
        if a in ids and b in ids and a != b:
            out.append(r.model_copy(update={"learning_id": ids[a], "related_learning_id": ids[b]}))
    return out


GROUND_PROMPT = """You audit a team's stored code-review "learnings" against the real
codebase, one small group at a time. Your job is to find which learnings are still TRUE
and USEFUL, and which are noise.

For each learning you were given:
1. Find what it actually refers to in the code. Use the search and file-reading tools.
   Look at the file_paths/file_pattern it claims, but do not stop there.
2. Decide a verdict:
   - corroborated: the code confirms this learning still applies.
   - stale: it was true once, but the pattern it refers to is gone or refactored.
   - contradicted: the codebase consistently does the OPPOSITE, and deliberately.
   - unfalsifiable: too vague to test against any code ("write clean code").
   - ungrounded: you could not find any code referent. Use this when the learning may be
     about process or deployment rather than code. This is NOT a criticism of the learning.
3. EVERY verdict except ungrounded and unfalsifiable MUST include at least one citation: a
   real file path, a line number, and a quote copied EXACTLY from that file. Verdicts whose
   quote cannot be found in the cited file are thrown away automatically. Do not paraphrase
   quotes. Do not cite a file you did not read.
4. If the verdict is unfalsifiable, supply suggested_hint_text: the sharpened, checkable
   version of the idea, concrete and testable against code. A rewrite proposal without
   replacement text changes nothing once a human approves it.

Use the investigate tool for a side question you would otherwise spend several rounds on
(where else is X used? does Y still exist?); it runs in a fresh context and reports back.
Return one verdict per learning you were given.

Prefer saying "I could not verify this" over inventing a justification. A wrong archive
destroys real team knowledge."""

SCOUT_PROMPT = """You map learnings to the code they govern; you do not judge them. For
each learning, find where it lives (search_code, outline_file, graph tools). Group
learnings that point at the same files or module, at most 5 per group, and give each group
a short `area`. In `notes`, per learning id, say where to look and anything obvious (file
deleted, pattern not found, too vague to test). Every learning id must appear in exactly
one group."""

RELATE_PROMPT = """You compare learnings that sound similar. Mark `duplicate_of` only when
two say the same thing; `related_learning_id` is the one that should SURVIVE (clearer,
better evidenced), and `suggested_hint_text` is the survivor's wording once it absorbs the
duplicate. Mark `conflicts_with` when they give contradictory advice; never pick a winner.
Say nothing about pairs that are merely related. An empty list is the normal answer."""

VERIFY_PROMPT = """You are the last check before knowledge is removed. For each proposal
(archive or merge), read the cited code yourself and confirm only if the claim holds;
default to not confirmed when unsure. A wrong archive destroys real team knowledge."""

INVESTIGATE_PROMPT = """Answer one focused question about this codebase with evidence:
exact file paths, lines and quotes. Do not judge learnings; report facts."""


class InvestigationReport(BaseModel):
    answer: str


def build_investigate_tool(run_agent, read_tools: list, counter: dict):
    from langchain_core.tools import tool

    @tool
    async def investigate(question: str) -> str:
        """Spawn a fresh sub-agent to answer ONE focused question about the codebase
        (where is X used? does Y still exist? what does Z do?) with file/line/quote
        evidence. Use it instead of spending several of your own rounds on a side question."""
        counter["n"] += 1
        report = await run_agent(f"investigate:{counter['n']}", INVESTIGATE_PROMPT,
                                 question, InvestigationReport, INVESTIGATE_MAX_ROUNDS, read_tools)
        return report.answer if report is not None else "the investigation could not complete"

    return investigate
