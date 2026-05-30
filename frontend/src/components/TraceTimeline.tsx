import { useState } from "react";
import type { TraceLLMRound, TraceToolCall } from "../api/types";
import { buildTimeline, type TimelineGroup } from "../lib/pipelineGraph";

interface Props {
  toolCalls: TraceToolCall[];
  llmRounds: TraceLLMRound[];
}

function RoundRow({ group, index, defaultOpen }: { group: TimelineGroup; index: number; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const { round, toolCalls } = group;
  const hasError = round?.error != null;

  return (
    <div className={`tl-round${open ? " open" : ""}`}>
      <div
        className={`tl-round-head${hasError ? " err" : ""}`}
        onClick={() => setOpen((o) => !o)}
      >
        <div className="tl-dot">{index + 1}</div>
        <span className="tl-caret">▶</span>
        <span className="tl-round-title">Round {index + 1}</span>
        {hasError ? (
          <span className="tl-err-msg">{round!.error}</span>
        ) : (
          toolCalls.length > 0 && (
            <span className="tl-toolcount">
              {toolCalls.length} tool call{toolCalls.length === 1 ? "" : "s"}
            </span>
          )
        )}
        {round != null && !hasError && (
          <div className="tl-round-meta">
            <span>
              {round.prompt_tokens ?? "—"} prompt · {round.completion_tokens ?? "—"} compl.
            </span>
            <span>{round.latency_ms != null ? `${round.latency_ms} ms` : "—"}</span>
          </div>
        )}
      </div>
      {toolCalls.length > 0 && (
        <div className="tl-tools">
          {toolCalls.map((tc, i) => (
            <div key={i} className={`tl-tool${tc.status === "error" ? " err" : ""}`}>
              <span className="tl-tool-status" />
              <span className="tl-tool-name">{tc.tool}</span>
              <span className="tl-tool-dur">
                {tc.duration_ms != null ? `${tc.duration_ms} ms` : "—"}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function TraceTimeline({ toolCalls, llmRounds }: Props) {
  const groups = buildTimeline(toolCalls, llmRounds);

  if (groups.length === 0) {
    return <p className="muted">No LLM rounds recorded for this node.</p>;
  }

  return (
    <div className="tl">
      {groups.map((group, index) => (
        <RoundRow key={index} group={group} index={index} defaultOpen={index === 0} />
      ))}
    </div>
  );
}
