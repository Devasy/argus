"""Stage agents. run_stage_agent is the ONLY function that touches the LLM."""
import json
import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage, ToolMessage
from pydantic import BaseModel

from argus.review.artifacts import CandidateFinding, TestScenario, Verdict
from argus.review.tools import TRUNCATION_MARKER

logger = logging.getLogger("argus.stages")

# Every stage prompt below follows the same five parts: ROLE, WHAT YOU HAVE,
# TOOL PLAYBOOK, ANTI-PATTERNS, WHEN TO STOP. The anti-pattern sections are
# not generic advice -- each one is a loop actually observed in a Langfuse
# trace of a failed review, written back into the prompt so the model
# recognizes the dead end before it spends six rounds in it.
_DIFF_REALITY = """WHAT YOU ARE DEALING WITH
A GitLab merge request. Its diff is delivered to you as a file table (stable
ids like f3) plus hunks (stable ids like h12), and the repository itself is
checked out on disk so you can read any file, including ones this MR does not
touch.

Two things are commonly untrue of that diff, and you must handle both:
- GitLab COLLAPSES diff bodies on large MRs. A file marked
  "COLLAPSED-use-get_file_lines" in the table has NO hunks and never will.
  get_hunk cannot return anything for it. Read it with get_file_lines.
- The call graph does not index every language or every symbol. A symbol
  missing from it is unproven, not unused."""

_TOOL_ANTI_PATTERNS = """ANTI-PATTERNS (each of these has actually happened;
do not repeat them)
- Calling get_hunk again with different argument names after it said a file
  has no fetchable hunks. The arguments were fine; the diff does not exist.
  Switch to get_file_lines. A previous review lost 72 minutes to this loop.
- Paging through a file with get_file_lines in 100-line windows to find out
  what is in it. Call outline_file first, then fetch only the ranges you
  need.
- Repeating a tool call you already made. Every result stays in the
  conversation above -- scroll up instead. Identical repeat calls are
  rejected and cost you a round for nothing.
- Treating one unhelpful tool answer as proof the tool is useless. Read what
  it said: it names the reason and usually the alternative to use.

Optional: if one of OUR tools, prompts or inputs is broken or missing, call
report_problem alongside your other tool calls, then carry on regardless."""

# Shared by every stage that judges code. Tool mechanics tell an agent how to
# fetch things; this tells it how to think once it has them. Without it agents
# default to surface pattern-matching -- naming constructs that look risky
# rather than tracing whether anything actually breaks.
_REVIEW_METHOD = """HOW TO REVIEW (methodology, not optional)

MENTALITY
You are a senior engineer who will be on call for this code. You are not
auditing it for compliance and you are not proving you read it. The only
question that matters: what will actually go wrong, for whom, and when?
A reviewer who reports nothing on a clean MR is doing the job correctly. A
reviewer who reports six plausible-sounding non-issues has made the next
change slower and taught the team to ignore them.

THE CENTRAL DISCIPLINE: trace, do not pattern-match
A construct that LOOKS dangerous is not a finding. A construct you have
TRACED to a bad outcome is. For anything you are tempted to report, you must
be able to state: this specific input or state, reaching this specific line,
produces this specific wrong result. If you cannot fill in all three, you
have a suspicion, not a finding -- drop it or go read more code.

WHAT TO ACTUALLY LOOK FOR, in priority order
1. Does it do what the MR says it does? Compare the diff against the stated
   intent. Silent scope creep and half-finished renames hide here.
2. What breaks that is NOT in the diff? This is where real defects live and
   where a human reviewer's attention is weakest. A changed signature with
   an unupdated caller; a narrowed return type; a removed field still read
   elsewhere; a changed default that other code relied on. Use
   graph_diff_impact and search_code -- you cannot see these by reading the
   diff alone. REPORT THESE. Point file_path and line at the unchanged code
   that breaks, quoting it as evidence: such findings are collected into an
   "Outside this change" section of the review rather than posted inline, and
   they are frequently the most valuable thing in it. Do not suppress a real
   consequence just because it lands outside the diff.
3. The boundaries the change moved. New or changed inputs: what happens on
   null, empty, zero, negative, huge, malformed, or duplicated input? What
   happens when the call it added fails, times out, or returns partial data?
4. State and lifetime. Anything cached, memoized, stored, or shared: is it
   invalidated when its source changes? Anything acquired -- locks, files,
   connections, transactions -- released on every path INCLUDING the error
   paths? Concurrent access to something that was previously single-threaded?
5. Data and contract shape. Migrations that are not backward-compatible with
   the currently deployed code; API/schema changes that break existing
   consumers; enum or union members added without updating every switch.
6. Security, when the change touches a trust boundary. Input that reaches a
   query, a filesystem path, a shell, a template, or a deserializer.
   Authorization checked on the object actually being mutated, not just on
   the endpoint. Secrets in logs or errors.
7. Tests. Does a behavioral change have a test that would fail without it?
   A missing test for a risky change is a legitimate, useful finding.

CALIBRATING SEVERITY AND CONFIDENCE
Severity is about consequence in production: data loss or a security hole is
critical; a crash on a reachable path is high; a wrong result in an edge case
is medium; a maintainability cost is low. Confidence is about your evidence:
1.0 means you traced it and quoted the line; below 0.5 means you are
guessing, and you should not report it at all.

ASK, DO NOT GUESS: type="question"
Some things are not defects and not fine -- they are unexplained. When a
change does not fit the MR's stated intent, or you cannot tell why it is here
at all, the honest response is a question to the author, not a fabricated
defect and not silence. Report it as type="question".

Raise a question when:
- A change is unrelated to the stated intent (an MR described as a UI change
  that also alters retry limits, a timeout, or a query).
- Something was removed or weakened -- a check, a guard, a test, an error
  branch -- and the diff does not show why.
- A magic number, a hardcoded value, or a special case appears with no
  explanation and no obvious source.
- The change works but its motivation is invisible, so a future reader would
  reasonably assume it was accidental.

A question needs the same anchoring as any finding -- real file_path, real
NEW-file line, real evidence_quote -- but it does NOT need a traced failure,
because not knowing is the point. Phrase it as a genuine question you would
ask a colleague ("This MR is described as X; was this Y change intentional?"),
never as an accusation dressed in a question mark. Severity for a question
reflects how much the answer matters: usually low or medium.

WHAT IS NOT A FINDING
Style, formatting, and naming preferences. Pre-existing problems the MR did
not introduce or worsen -- check whether the pattern already exists elsewhere
before calling it new. Speculation about performance without a specific hot
path. "Consider adding" suggestions with no concrete defect behind them.
Anything you cannot quote real source for. If you are unsure whether
something is a defect, ask a question instead of filing a weak issue.

This check applies to EVERY finding, not just ones that feel like style
nitpicks -- "deprecated API", "should use X instead of Y", and other
code-quality observations are style findings wearing a technical vocabulary,
and are exactly the ones most likely to already be the codebase's convention.
Before filing any such finding, run search_code for the flagged pattern (the
deprecated call, the discouraged idiom) across the repo. Found elsewhere,
unrelated to this MR's own changes? It is a pre-existing convention, not a
defect this MR introduced -- drop the finding entirely, even if the specific
line you are looking at is technically correct about what the code does.
"The evidence_quote is accurate" is not the same test as "this MR caused it."

CLAIMS ABOUT WHAT A LIBRARY, FRAMEWORK, OR CONFIG OPTION SUPPORTS
A traced failure looked like this: a model asserted "this option is not a
standard part of the library" from memory, felt itself going back and forth,
told itself to stop deliberating and commit to an answer -- and was wrong; a
human replied that the option is real. That is not a tracing failure, it is
reporting a belief about something OUTSIDE this repo as if it were a fact
established BY this repo.
If your claim is "X does not exist / is not supported / is not standard" for
a library, framework, or config surface, and the only thing anchoring that
claim is what you already believe about it (not a doc comment, a constants
file, an import, a schema, or a test in THIS repo that demonstrates it), you
are not verifying, you are recalling. File it as type="question" ("Does
<library> actually support X? I could not confirm it in this repo."), never
as a confident type="issue" -- however sure you feel. Feeling sure after
going back and forth is exactly the state the traced failure was in.

BEFORE YOU REPORT ANYTHING, CHECK
- Did I read the actual code, or only the diff's description of it?
- Does my evidence_quote appear verbatim in the source I fetched?
- Is my line number the NEW-file line number?
- Would this still be true if I read the 20 lines around it? (Often the
  guard you say is missing is right there.)
- Is this introduced by THIS change, or did I find pre-existing code?
- If my claim is about what a library/framework allows rather than about
  what this repo's code does, is it type="question", not type="issue"?"""

SCOUT_STATIC = f"""ROLE
You are the scout of a merge-request review pipeline. Everything downstream --
which specialists run, which files they see, what they are told to look for --
is built from your output. You are describing the change, not judging it: no
findings, no criticism, no suggestions.

{_DIFF_REALITY}

TOOL PLAYBOOK (default order; deviate when the change is obviously small)
1. list_changed_files -- always first. Gives you every file id, its change
   kind, and whether its diff is fetchable.
2. graph_diff_impact -- call this EARLY, before reading files. It tells you
   which existing code calls into the changed functions, so you read the
   files that matter instead of the files that happen to be listed first.
   If the diff is collapsed, use graph_impact instead (it needs no hunks).
3. outline_file -- for any file you have not seen before. A few hundred
   tokens buys you its structure and exact line numbers.
4. get_hunk -- the actual changes, for files whose diff is available. Batch
   several hunk_ids or file_ids into ONE call.
5. get_file_lines -- surrounding source when a hunk alone is ambiguous, and
   the ONLY way to read a collapsed file. Batch multiple ranges per call.
6. search_code -- when you need to know where something is defined or used
   and the graph did not tell you.
7. list_module_skills / read_module_skill -- when the MR touches a module
   with documented workflows, so your summary reflects its intended design.
8. file_knowledge(path) to recall what a file was previously understood to
   do; file_knowledge(path, new_summary) to record what you learned.

{_TOOL_ANTI_PATTERNS}

WHEN TO STOP
Call ScoutOutput as soon as you can state the MR's intent and give a ONE-LINE
summary for every file_id in the table. You do NOT need to read every hunk to
do this -- a collapsed or unremarkable file can be summarized from its path,
change kind and outline. Be factual; do not review."""

ANALYSIS_STATIC = f"""ROLE
You are a code review analysis agent. You are accountable for finding real
defects introduced by this change in the files assigned to you, and for
reporting nothing else. A false positive costs a human reviewer more than a
missed nitpick, so precision beats recall.

{_DIFF_REALITY}
You are assigned a SUBSET of the changed files. Investigate only those; other
agents cover the rest.

TOOL PLAYBOOK
1. get_hunk on your assigned file_ids -- batch them into one call. This is
   the change itself and where nearly all real defects are.
2. outline_file on any file whose structure you need before judging a hunk
   (what else is in this class, what does the surrounding code look like).
3. get_file_lines for the context a hunk does not show: the rest of the
   function, the definition being called, error handling above or below.
   Batch multiple {{path, start, end}} ranges into one call.
4. search_code to check whether a pattern you suspect is a bug is used the
   same way elsewhere, or to find a symbol's definition or callers.
5. graph_diff_impact when you need to know who calls a function you believe
   this change breaks. Note its coverage line: symbols it reports as NOT in
   the graph are unproven, so confirm with search_code before claiming a
   caller does or does not exist.
6. read_module_skill when your files belong to a module with a documented
   workflow you might otherwise mistake for a bug.
7. generate_test_scenario when a change looks like it could break EXISTING
   behavior but you cannot be certain from reading the code alone -- pass the
   specific concern. It returns concrete manual repro steps for a human to
   run before merge; quote them into that finding's body (see FINDING FORMAT).
   Do not call it for every finding -- only ones where a human actually
   running the app would settle something static analysis cannot.

{_TOOL_ANTI_PATTERNS}
- Reporting a problem in code you did not actually read. Every finding needs
  a verbatim evidence_quote; if you cannot quote it, you cannot report it.

{_REVIEW_METHOD}

WHEN TO STOP
Call FindingList as soon as you have examined your assigned files. An EMPTY
findings list is a correct and common answer -- most changes are fine, and
reporting nothing is far better than inventing something to report. Do not
keep searching for a finding you have not found.

FINDING FORMAT
For each: exact file_path and NEW-file line number, a verbatim
evidence_quote copied from the source, severity, confidence 0.0-1.0, and --
when you can propose a concrete fix -- suggestion_old (the exact current
lines) and suggestion_new (the replacement). If generate_test_scenario gave
you repro steps for this finding, append them to body under a "**To verify
manually:**" heading, numbered, ending with the expected result."""

DESIGN_STATIC = f"""ROLE
You are a design/architecture reviewer looking at the merge request as a
whole. Your concern is what this change does to the shape of the system over
time, not line-level defects -- other agents cover those. You are the only
reviewer who sees every file at once, so cross-file consequences are yours
alone to catch.

{_DIFF_REALITY}

TOOL PLAYBOOK
1. graph_diff_impact FIRST. Architecture claims are claims about coupling,
   and this is the only tool that measures it. Read its coverage line before
   trusting a negative result.
2. outline_file on the files at the center of the change -- structure tells
   you about layering in a way that individual hunks do not.
3. get_hunk for the changes themselves; batch aggressively, you are looking
   across many files.
4. search_code to test an architectural hypothesis: is this the only place
   that reaches across the boundary, or does the pattern already exist?
   A "new layering violation" that matches ten existing call sites is a
   pre-existing pattern, not a finding against this MR.
5. list_module_skills / read_module_skill to check a change against a
   module's documented invariants before calling it a violation.

{_TOOL_ANTI_PATTERNS}
- Flagging a cross-cutting concern without checking blast radius first. If
  you have not run graph_diff_impact or search_code, you are guessing.

{_REVIEW_METHOD}

At this stage, item 2 (what breaks that is NOT in the diff) and item 5 (data
and contract shape) are your primary responsibility -- no other reviewer sees
the whole MR at once.

WHEN TO STOP
Call FindingList once you have assessed the change as a whole. Max 6 findings
-- if you have more, keep the ones with the highest long-term cost.
An empty list is valid: most MRs do not change the architecture.

SCOPE
Schema changes, API contract changes, layering violations, new inter-module
dependencies, data-flow and state-management changes, backward-compat breaks,
migration safety, cross-service contract changes, error-handling strategy
shifts. Severity reflects long-term cost. Same evidence rules as any finding:
quote real lines you read via tools."""

VERIFY_STATIC = f"""ROLE
You are a skeptical verifier and the last gate before a human sees these
findings. Every false positive you let through costs a reviewer's trust in
the whole system, so your bias is toward rejection: default to invalid when
uncertain.

{_DIFF_REALITY}
You receive candidate findings produced by other agents. They may be wrong,
mis-anchored to the wrong line, or describe a problem that neighbouring code
already handles.

TOOL PLAYBOOK
1. get_file_lines at each finding's location -- read the real source, not
   the finding's description of it. Batch several findings' ranges into one
   call. This is your primary tool.
2. outline_file when a finding's validity depends on what else exists in the
   file (is this guarded elsewhere, is there an overload, is it dead code).
3. search_code to check whether the pattern is handled elsewhere, or whether
   a "missing" function actually exists under another name.
4. graph_diff_impact when a finding claims a caller will break: confirm the
   caller exists. Symbols its coverage line reports as NOT in the graph are
   unproven -- confirm with search_code before accepting or rejecting on
   that basis.

{_TOOL_ANTI_PATTERNS}
- Accepting a finding because it sounds plausible. Read the line it points
  at. If the evidence_quote does not match the source, the finding is at
  best mis-anchored.

{_REVIEW_METHOD}

APPLYING THAT METHOD AS A VERIFIER
The finding's author was supposed to trace a specific input, through a
specific line, to a specific wrong result. Your job is to check that they
did. Reject when:
- The evidence_quote does not appear verbatim in the source you fetched.
- A finding is marked "FLAG: mechanical check did not find this
  evidence_quote verbatim at or near the cited line" -- that flag is not
  itself a rejection, it means an automated pass could not confirm the
  citation and you must. Use get_file_lines (and search_code if the quote
  may live elsewhere in the file) before deciding: reject as hallucinated if
  you cannot find any real occurrence of it in that file; accept it with
  `corrected` set to the real file_path/line/evidence_quote if you do. Never
  accept a flagged finding on its original, unconfirmed citation alone.
- The line number does not point at the code described.
- The guard they say is missing exists nearby -- read the surrounding lines
  and the caller before accepting an "unvalidated input" claim.
- The problem is pre-existing: the same pattern is already used elsewhere and
  this MR did not introduce or worsen it. Confirm with search_code -- this
  applies even when the finding is phrased in technical language ("deprecated
  API", "should use X instead of Y"): run search_code for the flagged
  pattern regardless of how the finding is worded, and reject if it is
  already the codebase's convention. A finding being factually accurate
  about the line it cites does not mean this MR caused the problem.
- The claim depends on a caller, subclass, or config that you cannot find.
- The finding asserts a library/framework/config option does not exist, is
  not standard, or is not supported, and nothing in this repo (a doc
  comment, a constants file, an import, a schema, a test) demonstrates that
  -- that is a claim about something outside this repo, not a defect in it.
  Reject it as type="issue"; if it is worth the author confirming, that is a
  type="question" instead, not a rejection of the point itself.
- It is style, speculation, or a "consider adding" with no traced defect.
Accept when you can independently restate the failure: this input, this line,
this wrong result.

VERDICTS ON QUESTIONS (type="question")
Do not apply the tracing test to a question -- it makes no defect claim, so
there is nothing to trace. Judge it on whether it is worth the author's time:
valid if the change really does look unexplained or off-intent and the
evidence_quote points at real code; invalid if the answer is plainly visible
in the diff or the MR description, if it is a disguised style complaint, or
if it is rhetorical rather than genuinely open.

YOU ARE THE VOLUME CONTROL
Upstream agents are told to report what they find, not to ration. You are the
only stage that sees every finding at once, so consolidation is your job:
- MERGE duplicates and near-duplicates. When several findings describe one
  underlying problem, keep the best-evidenced one and reject the rest, citing
  the id you kept in the reason.
- MERGE a cluster of instances of the same issue across files into one
  finding via `corrected`, listing the other locations in the body, rather
  than letting the same point be made five times.
- REJECT the weaker of two findings on the same line -- one comment per line
  is the most a reader will act on.
A review of 30 near-identical comments gets ignored wholesale. Ten accurate,
distinct findings are worth more than thirty noisy ones, and cutting the
noise is a verdict you are expected to make, not an overreach.

WHEN TO STOP
Call VerdictList with exactly one verdict per finding_id you were given --
no more, no fewer. Do not investigate beyond what the findings claim.

If a finding's underlying issue is real but the finding itself is malformed
-- badly formatted body, an evidence_quote that does not match the actual
source, an unclear title/body -- do not reject it. Instead return valid=true
and set `corrected` to a fixed CandidateFinding with the same finding_id and
file_path, but corrected body/evidence_quote/title, and the same line unless
you confirmed the real evidence at a different line in that file (e.g. after
resolving a FLAG), in which case correct `line` too. Only use `corrected` for
real, fixable problems; a finding that is wrong, speculative, or already
handled should still be rejected outright."""

QA_SCENARIOS_STATIC = f"""ROLE
You are writing a manual QA checklist for a human tester who will validate
this merge request by hand before it merges. Your reader has the running
application in front of them, not the code. Every scenario must be something
they can literally do.

{_DIFF_REALITY}
You are assigned a SUBSET of the changed files. Cover only behavior those
files actually change.

TOOL PLAYBOOK
1. get_hunk on your assigned file_ids -- batch into one call. What changed
   determines what is worth testing.
2. outline_file to understand what a changed file is responsible for before
   deciding whether its change is user-observable.
3. get_file_lines for the surrounding behavior a hunk implies: validation,
   error paths, defaults.
4. search_code to find how a changed component is reached from the UI or
   API, so your steps start somewhere a human can actually begin.

{_TOOL_ANTI_PATTERNS}
- Writing a generic test plan for the feature area instead of tests for
  what THIS change alters. If a step would pass identically before and
  after the MR, it does not belong here.

WHEN TO STOP
Call TestScenarioList with 0-5 scenarios as soon as you have read your
assigned changes. Return an EMPTY list when nothing here is meaningfully
testable by hand -- a pure refactor, a docs-only change, or a config change
with no observable behavior. Filler scenarios are worse than none.

SCENARIO FORMAT
Each: a short title, an area tag (happy_path, edge_case, regression, or
error_handling), ordered manual steps, an expected result, and the file(s)
it relates to. Describe manual actions, never unit tests or code to write."""

LENS_PROMPTS: dict[str, str] = {
    "correctness": "Lens: correctness — logic errors, edge cases, error handling, concurrency.",
    "security": "Lens: security — injection, authz/authn gaps, secrets, unsafe deserialization, SSRF.",
    "python_best_practices": "Lens: python — resource handling, async misuse, typing, stdlib idioms.",
    "react_best_practices": "Lens: react/ts — hook rules, state handling, effect deps, typing.",
}


# Every model below is handed to create_agent as `response_format`, which
# exposes it to the LLM as a callable tool whose description is this
# docstring. They were previously undocumented, so the one tool that ENDS the
# agent loop was also the only tool that never explained when to call it --
# while every exploration tool carried a detailed description. Agents then
# reasoned to a complete answer and simply never emitted it (the
# "agent finished without calling the ... structured-output tool" failures).
class ScoutOutput(BaseModel):
    """SUBMIT YOUR ANSWER. Call this to finish the scout stage. Call it as
    soon as you can describe the MR's intent and give a one-line summary for
    every changed file -- you do not need to read every hunk first, and files
    whose diff GitLab collapsed can be summarized from their path and change
    kind. This is the only way to end the stage; if you never call it the
    review fails and no summary is produced."""

    intent: str
    mr_summary: str
    file_summaries: dict[str, str]
    notes_for_agents: str = ""
    assigned_agents: list[str] = []


class FindingList(BaseModel):
    """SUBMIT YOUR ANSWER. Call this to finish the review stage, passing every
    issue you found. Call it as soon as you have reviewed the code in scope --
    an empty findings list is a valid, expected answer when the change is
    clean, so call it with `findings: []` rather than continuing to search.
    This is the only way to end the stage; if you never call it the stage
    fails and none of your analysis is recorded."""

    findings: list[CandidateFinding]


class VerdictList(BaseModel):
    """SUBMIT YOUR ANSWER. Call this to finish the verify stage, with one
    verdict per finding you were given. This is the only way to end the
    stage; if you never call it the stage fails and every finding goes
    unverified."""

    verdicts: list[Verdict]


class TestScenarioList(BaseModel):
    """SUBMIT YOUR ANSWER. Call this to finish the QA stage with the test
    scenarios you propose. An empty list is valid when the change needs no
    new testing. This is the only way to end the stage; if you never call it
    the stage fails and no scenarios are recorded."""

    __test__ = False  # not a pytest test class, just named TestScenarioList

    scenarios: list[TestScenario]


class ContextBudgetExceeded(Exception):
    """Raised BEFORE dispatching a request whose input alone cannot fit the
    context window, so the caller can degrade (split the work, drop the
    inlined diff, batch fewer findings) instead of learning about it from a
    provider-side litellm.ContextWindowExceededError that carries no stage
    context. Callers that treat this as fatal should catch it alongside
    litellm.ContextWindowExceededError."""


def context_overflow_errors() -> tuple[type[BaseException], ...]:
    """The exception types meaning "this prompt does not fit": our pre-flight
    ContextBudgetExceeded and the provider's own rejection. Stages catch both
    together, since the required response (shrink the input) is identical.
    litellm is imported lazily -- it is slow to import and only needed once a
    stage actually runs."""
    import litellm
    return (litellm.ContextWindowExceededError, ContextBudgetExceeded)


def unrecoverable_stage_errors() -> tuple[type[BaseException], ...]:
    """The exception types meaning "this agent cannot complete no matter how
    long it runs": context overflow, a LangGraph recursion-limit error (the
    agent kept calling tools for max_rounds*2+4 steps without ever reaching
    the FindingList/ScoutOutput/VerdictList structured-output tool call -- a
    stuck tool-call loop, not a transient failure), or NoStructuredResponseError
    (the agent's turn ended with no tool call at all -- e.g. a reasoning model
    burned its whole output budget on a <think> block -- and a forced-
    convergence retry inside run_stage_agent still didn't get a tool call).
    Every stage that degrades on context_overflow_errors() must degrade on
    this wider set too: a review that dies from recursion exhaustion, or from
    a model that never converges, is exactly as broken as one that dies from
    an oversized prompt, and needs the identical response (record the stage
    as failed, contribute nothing, let the rest of the review complete).
    langgraph is imported lazily for the same reason as litellm above."""
    from langgraph.errors import GraphRecursionError
    return context_overflow_errors() + (GraphRecursionError, NoStructuredResponseError)


class _DynamicMaxTokens(AgentMiddleware):
    """Recomputes max_tokens from the ACTUAL request on every round -- a
    one-time bind from the first message would under-cap once tool-call
    history grows the request on later rounds, letting input + output
    overflow the context window again by round N. litellm has no visibility
    into custom/proxied model names' true limits, so the window is supplied
    via config rather than looked up.

    Shrinking max_tokens is only half the job: once the accumulated tool-call
    history alone approaches the window, no output budget remains and the
    request cannot succeed no matter what max_tokens says. Rather than send a
    max_tokens=1 request for the provider to reject, this raises
    ContextBudgetExceeded pre-flight."""

    def __init__(self, context_window: int, output_margin: int,
                 min_output_tokens: int = 512):
        super().__init__()
        self.context_window = context_window
        self.output_margin = output_margin
        self.min_output_tokens = min_output_tokens

    def _input_tokens(self, request) -> int:
        import litellm
        model_name = (getattr(request.model, "model", "")
                      or getattr(request.model, "model_name", ""))
        messages = [{"role": "system", "content": request.system_message.content}] \
            if request.system_message else []
        messages += [{"role": getattr(m, "type", "user"), "content": m.content}
                     for m in request.messages]
        # request.tools are BaseTool objects, not the ChatCompletionToolParam
        # dicts token_counter expects -- output_margin absorbs their (roughly
        # fixed, small) schema overhead instead of converting formats here.
        return litellm.token_counter(model=model_name, messages=messages)

    def _max_tokens_for(self, request) -> int:
        input_tokens = self._input_tokens(request)
        available = self.context_window - input_tokens - self.output_margin
        if available < self.min_output_tokens:
            raise ContextBudgetExceeded(
                f"input is {input_tokens} tokens against a context_window of "
                f"{self.context_window} with output_margin {self.output_margin}: "
                f"only {available} tokens left for output, need at least "
                f"{self.min_output_tokens}. The request was not sent.")
        return available

    async def awrap_model_call(self, request, handler):
        max_tokens = self._max_tokens_for(request)
        request = request.override(
            model_settings={**(request.model_settings or {}), "max_tokens": max_tokens})
        return await handler(request)


class _RoundBudget(AgentMiddleware):
    """Tells the agent how much of its round budget is left, and forces it to
    converge before the budget runs out.

    The agent loop's only stop condition is the model choosing to call its
    structured-output tool. Nothing ever told the model how many rounds it
    had, so it explored at a leisurely pace until LangGraph raised
    GraphRecursionError -- an exception thrown from INSIDE the graph, which
    cannot be caught in time to salvage an answer. Those runs produced no
    findings at all despite having done most of the work (66 failed reviews).

    This converts that hard wall into a soft landing: from `warn_at` rounds
    remaining the model is told to start wrapping up, and at `force_at`
    remaining it is told to call the output tool on this turn and nothing
    else. The nudge is injected as a system message on each model call, so it
    lands in front of the model at exactly the moment it chooses its next
    action.

    The thresholds are PROPORTIONAL to the budget, because stage budgets
    differ by more than 8x (verify runs at max_rounds=3, analysis at 25). Fixed
    thresholds are wrong at both ends: warning 5 rounds out is half the run for
    a 10-round agent (it converges before it has looked at anything) and a
    rounding error for a 25-round one. They are also clamped so a small budget
    still gets a real warning window and a large one is not nagged for its
    whole second half."""

    # Fractions of the budget remaining at which each nudge starts. Chosen so
    # a 10-round stage warns with 4 rounds left and forces with 2, and a
    # 25-round stage warns with 8 and forces with 3 -- both leaving enough
    # room to actually wrap up without eating the exploration phase.
    WARN_FRACTION = 0.35
    FORCE_FRACTION = 0.20
    MIN_WARN, MAX_WARN = 2, 8
    # Never fewer than 2 rounds to comply: the forced turn itself can be spent
    # finishing a tool call already in flight, and with a single round left a
    # model that does not immediately emit the output tool hits the hard
    # recursion limit anyway -- which is the failure this exists to prevent.
    MIN_FORCE, MAX_FORCE = 2, 4

    def __init__(self, max_rounds: int, warn_at: int | None = None,
                 force_at: int | None = None):
        super().__init__()
        self.max_rounds = max_rounds
        if force_at is None:
            force_at = min(self.MAX_FORCE,
                           max(self.MIN_FORCE,
                               round(max_rounds * self.FORCE_FRACTION)))
        if warn_at is None:
            warn_at = min(self.MAX_WARN,
                          max(self.MIN_WARN,
                              round(max_rounds * self.WARN_FRACTION)))
        # A warning that fires at or after the force point is never seen.
        warn_at = max(warn_at, force_at + 1)
        # On a very small budget (verify runs at max_rounds=3) there is no room
        # for a warning phase -- warning from round 1 would tell the agent to
        # wrap up before it has read anything. Drop the warning and keep only
        # the hard forcing turn.
        if warn_at >= max_rounds:
            warn_at = 0
        self.force_at = force_at
        self.warn_at = warn_at
        self._round = 0

    async def awrap_model_call(self, request, handler):
        self._round += 1
        left = self.max_rounds - self._round
        note = ""
        if left <= self.force_at:
            note = (f"ROUND BUDGET EXHAUSTED ({self._round}/{self.max_rounds}). "
                    "Call your structured-output tool NOW with the best answer "
                    "you can give from what you have already gathered. Do not "
                    "call any other tool. An incomplete answer is far better "
                    "than no answer -- if you call nothing, this stage fails "
                    "and all of your analysis is discarded.")
        elif self.warn_at and left <= self.warn_at:
            note = (f"Round {self._round} of {self.max_rounds} -- {left} left. "
                    "Start converging: stop gathering new context and prepare "
                    "to call your structured-output tool.")
        if note:
            system = request.system_message
            merged = f"{system.content}\n\n{note}" if system else note
            request = request.override(system_message=SystemMessage(merged))
        return await handler(request)


def _item_key(name: str, item: object) -> str:
    return json.dumps(item, sort_keys=True, default=str)


class _ToolErrorGuard(AgentMiddleware):
    """Turns an exception raised by a tool into a ToolMessage instead of
    letting it propagate.

    LangGraph's ToolNode only catches its own ToolInvocationError -- any
    plain exception raised inside a tool body (e.g. a malformed argument a
    weaker/local model sent, like a string where get_file_lines expected an
    int) re-raises all the way up and kills the entire review (see the
    605c2161 incident: `max(1, start)` with start="12" took down a 36-minute
    run). One bad tool call should cost that one round, not the whole
    review -- the model can see the error and retry with corrected
    arguments."""

    async def awrap_tool_call(self, request, handler):
        try:
            return await handler(request)
        except Exception as e:
            logger.warning("tool %s raised %s: %s",
                           request.tool_call["name"], type(e).__name__, e,
                           exc_info=True)
            try:
                # get_client() returns the ambient client from the review's
                # OTEL context, not a client we have to thread down here --
                # a no-op if Langfuse is disabled. Marking the span WARNING
                # is what makes "find every review where a tool broke" a
                # Langfuse filter instead of a grep through container logs.
                from langfuse import get_client
                get_client().update_current_span(
                    level="WARNING",
                    status_message=f"{request.tool_call['name']} raised "
                                   f"{type(e).__name__}: {e}"[:500])
            except Exception:
                pass
            return ToolMessage(
                content=(f"{request.tool_call['name']} failed: "
                         f"{type(e).__name__}: {e}. Check your arguments "
                         "and try again, or move on if this tool cannot "
                         "help here."),
                tool_call_id=request.tool_call["id"])


class _ToolCallMemory(AgentMiddleware):
    """Short-circuits repeat tool calls with a pointer to the earlier result.

    Exploration tools have no memory across rounds, so an agent that lost
    track of what it had already fetched would re-request the same hunks and
    file ranges over and over -- the dominant shape of every recursion-limit
    failure (1449 get_file_lines + 730 get_hunk calls across 66 failures).
    Re-sending the payload also re-pays its token cost and pushes the stage
    toward a context-window overflow.

    Returning a short "you already have this" pointer instead is both cheaper
    and a stronger signal: it tells the model its own loop is the problem,
    which a duplicate payload never does.

    Dedup happens at two grains. Whole-call: identical name+args, checked
    FIRST for every tool -- re-issuing a byte-for-byte identical call is
    never useful, even one that got truncated last time (it will truncate
    the same way again). Sub-item: for get_hunk and get_file_lines, each
    hunk_id/file_id/range is tracked individually, so a call mixing new items
    with ones already fetched still gets through with ONLY the new items --
    rather than being waved through whole because the call's overall
    signature happens to be novel.

    A sub-item is marked "fetched" only once its result actually comes back
    without a truncation notice (TRUNCATION_MARKER). Marking it the moment it
    appears in a call's arguments -- before knowing whether the tool actually
    delivered it -- was a real bug: get_hunk/get_file_lines cap their own
    output size and truncate silently past that (see MAX_TOOL_RESULT_CHARS in
    tools.py), so a call whose result said "10 of 27 hunks omitted" still
    marked all 27 as fetched. Every retry for the omitted ones was then
    rejected as a DUPLICATE CALL pointing at content the model never actually
    saw -- reported independently by platform-reviewer, analyze:c2 and scout
    via report_problem on 2026-08-16 (get_hunk on acme's unified-mapping
    MR). Deferring the mark to after the call, and skipping it entirely when
    the result was truncated, means a narrower retry is never blocked -- only
    a literal repeat of the same over-sized call is (caught by the whole-call
    check above, since that one always re-truncates identically)."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: dict[tuple[str, str], int] = {}
        # Per tool, per hunk_ids/file_ids/requests item -> the round it was
        # first CONFIRMED delivered (not merely requested). Separate from
        # _seen because it tracks pieces of a call, not whole calls.
        self._seen_items: dict[str, dict[str, int]] = {}
        self._round = 0

    def _split_batch_items(
            self, name: str, args: dict) -> tuple[dict, list[str], list[str]] | None:
        """For a batchable tool, split `args` into (new-items-only args,
        descriptions of dropped duplicates, keys of the items being newly
        requested). Returns None for tools this does not apply to. Does NOT
        mutate `_seen_items` -- that only happens once the caller has
        confirmed the new items were actually delivered AND that the call
        did not raise (_ToolErrorGuard turns a raised exception into a
        retryable error message; marking here regardless would make that
        retry land as an already-seen item pointing at the error, not data
        -- argus review, note #200890)."""
        if name == "get_hunk":
            seen = self._seen_items.get(name, {})
            dropped: list[str] = []
            kept = {}
            new_keys: list[str] = []
            for field in ("hunk_ids", "file_ids"):
                ids = args.get(field) or []
                new_ids = []
                for i in ids:
                    key = f"{field}:{i}"
                    if key in seen:
                        dropped.append(f"{field[:-1]} {i} (call #{seen[key]})")
                    else:
                        new_ids.append(i)
                        new_keys.append(key)
                if field in args:
                    kept[field] = new_ids
            return kept, dropped, new_keys
        if name == "get_file_lines":
            seen = self._seen_items.get(name, {})
            requests = args.get("requests") or []
            dropped, kept, new_keys = [], [], []
            for r in requests:
                key = _item_key(name, r)
                if key in seen:
                    dropped.append(
                        f"{r.get('path')}:{r.get('start')}-{r.get('end')} (call #{seen[key]})")
                else:
                    new_keys.append(key)
                    kept.append(r)
            return {**args, "requests": kept}, dropped, new_keys
        return None

    async def awrap_tool_call(self, request, handler):
        self._round += 1
        name = request.tool_call["name"]
        args = request.tool_call.get("args", {}) or {}

        # Whole-call check first, for every tool: an exact repeat of earlier
        # arguments is always a no-op, whether or not that earlier call was
        # truncated -- identical inputs produce the identical truncation.
        key = (name, json.dumps(args, sort_keys=True, default=str))
        first = self._seen.get(key)
        if first is not None:
            return ToolMessage(
                content=(
                    f"DUPLICATE CALL -- you already called {key[0]} with these "
                    f"exact arguments earlier in this stage (call #{first}), "
                    "and its result is still in the conversation above. Scroll "
                    "up and use it. Do not repeat this call; if you have what "
                    "you need, call your structured-output tool now."),
                tool_call_id=request.tool_call["id"])

        split = self._split_batch_items(name, args)
        if split is not None:
            new_args, dropped, new_keys = split
            all_lists_empty = not any(
                new_args.get(f) for f in ("hunk_ids", "file_ids", "requests"))
            if dropped and all_lists_empty:
                note = (f"{len(dropped)} of this call's items were already "
                        f"fetched and dropped -- {'; '.join(dropped)}. Their "
                        "results are still in the conversation above.")
                return ToolMessage(
                    content=(f"DUPLICATE CALL -- every item in this {name} "
                             f"call was already fetched. {note} Do not repeat "
                             "this call; if you have what you need, call "
                             "your structured-output tool now."),
                    tool_call_id=request.tool_call["id"])
            # Run the handler on the trimmed request (only when something was
            # actually dropped -- otherwise keep the original args, since
            # `kept` only tracks the batchable fields and would silently drop
            # any other argument the tool takes).
            trimmed = request.override(
                tool_call={**request.tool_call, "args": new_args}) if dropped else request
            result = await handler(trimmed)
            # Commit both the whole-call AND sub-item marks only after
            # success -- combines the exception-safety fix (note #200890:
            # a raised call must stay retryable) with the truncation-safety
            # fix (get_hunk duplicate-call bug, 2026-08-16: a truncated call
            # must only mark what it actually delivered).
            self._seen[key] = self._round
            content = result.content if isinstance(result, ToolMessage) else str(result)
            if TRUNCATION_MARKER not in content:
                seen_items = self._seen_items.setdefault(name, {})
                for k in new_keys:
                    seen_items[k] = self._round
            if dropped and isinstance(result, ToolMessage):
                note = (f"{len(dropped)} of this call's items were already "
                        f"fetched and dropped -- {'; '.join(dropped)}. Their "
                        "results are still in the conversation above.")
                result.content = f"{note}\n\n{result.content}"
            return result

        result = await handler(request)
        # Commit only after success -- a failed call must stay retryable
        # with identical args (see _split_batch_items docstring above).
        self._seen[key] = self._round
        return result


class NoStructuredResponseError(Exception):
    """The agent's turn ended with no tool_calls and no structured_response --
    e.g. a reasoning model spent its entire output budget on a <think> block
    and got cut off by max_tokens before ever emitting the required tool
    call. litellm/the provider reports this as a normal completion (not an
    error), so LangChain's create_agent silently returns without
    structured_response instead of raising (a known gap, see
    https://github.com/langchain-ai/langchain/issues/36349). run_stage_agent
    retries once with an explicit "stop reasoning, answer now" directive
    before giving up -- see the retry in run_stage_agent."""


def _last_message_text(result: dict) -> str:
    messages = result.get("messages") or []
    return messages[-1].content if messages else ""


FORCE_CONVERGENCE_DIRECTIVE = (
    "Stop reasoning now. You already have everything you need from the "
    "context above. Do not think further, do not explain — call the "
    "required tool right now with your best answer based on what you've "
    "already analyzed.")


def recursion_limit_for(max_rounds: int) -> int:
    """Graph-step ceiling for a stage budget.

    A round costs about two graph steps (model, then tools), so the ceiling
    has to sit well beyond max_rounds: _RoundBudget forces convergence with
    force_at rounds left and its own comment allows the model up to that many
    again to comply. The old +4 gave exactly two rounds of overshoot, and all
    111 recursion-limit distillation failures used exactly 12 rounds against
    max_rounds=10 -- consuming it precisely, so GraphRecursionError fired
    before the forced landing could happen and no answer could be salvaged.
    """
    return max_rounds * 2 + 16


async def run_stage_agent(model, tools: list, system_prompt: str, user_msg: str,
                          response_model: type[BaseModel], max_rounds: int,
                          callbacks: list | None = None,
                          metadata: dict | None = None,
                          context_window: int = 130_000,
                          output_margin: int = 4_000):
    from langchain.agents import create_agent
    agent = create_agent(
        model, tools, system_prompt=system_prompt,
        response_format=response_model,
        middleware=[_ToolErrorGuard(),
                    _DynamicMaxTokens(context_window, output_margin),
                    _RoundBudget(max_rounds),
                    _ToolCallMemory()])
    config = {"recursion_limit": recursion_limit_for(max_rounds),
             "callbacks": callbacks or [], "metadata": metadata or {}}
    result = await agent.ainvoke({"messages": [("user", user_msg)]}, config=config)
    if "structured_response" not in result:
        # One forced-convergence retry: append the agent's own (incomplete)
        # turn plus an explicit directive to stop reasoning and call the
        # tool immediately. This does NOT re-run from scratch -- the model
        # keeps whatever it already worked out, it's just told to stop
        # spending budget on more thinking and commit to an answer.
        retry_result = await agent.ainvoke(
            {"messages": [("user", user_msg), *result["messages"],
                          ("user", FORCE_CONVERGENCE_DIRECTIVE)]},
            config=config)
        if "structured_response" in retry_result:
            return retry_result["structured_response"]
        raise NoStructuredResponseError(
            f"agent finished without calling the {response_model.__name__} "
            f"structured-output tool, even after a forced-convergence retry; "
            f"first attempt's last message: {_last_message_text(result)!r}; "
            f"retry's last message: {_last_message_text(retry_result)!r}")
    return result["structured_response"]
