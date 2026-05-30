import StatusBadge from "./StatusBadge";
import type { TraceLLMRound, TraceToolCall } from "../api/types";

interface Props {
  toolCalls: TraceToolCall[];
  llmRounds: TraceLLMRound[];
}

export default function TraceTables({ toolCalls, llmRounds }: Props) {
  return (
    <>
      <h4 className="section">Tool calls ({toolCalls.length})</h4>
      <div className="table-wrap section">
        <table className="nw-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Tool</th>
              <th>Status</th>
              <th>Duration</th>
            </tr>
          </thead>
          <tbody>
            {toolCalls.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  No tool calls recorded.
                </td>
              </tr>
            )}
            {toolCalls.map((tc) => (
              <tr key={tc.seq}>
                <td className="mono">{tc.seq}</td>
                <td className="mono">{tc.tool}</td>
                <td>
                  <StatusBadge status={tc.status} />
                </td>
                <td className="muted">{tc.duration_ms != null ? `${tc.duration_ms} ms` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h4 className="section">LLM rounds ({llmRounds.length})</h4>
      <div className="table-wrap section">
        <table className="nw-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Prompt tokens</th>
              <th>Completion tokens</th>
              <th>Latency</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {llmRounds.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No LLM rounds recorded.
                </td>
              </tr>
            )}
            {llmRounds.map((lr) => (
              <tr key={lr.seq}>
                <td className="mono">{lr.seq}</td>
                <td className="muted">{lr.prompt_tokens ?? "—"}</td>
                <td className="muted">{lr.completion_tokens ?? "—"}</td>
                <td className="muted">{lr.latency_ms != null ? `${lr.latency_ms} ms` : "—"}</td>
                <td className="muted">{lr.error ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
