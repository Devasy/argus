import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { useAuditVerdicts, useApproveVerdict, useRejectVerdict } from "../../api/queries";
import type { AuditVerdict } from "../../api/types";
import ErrorBox from "../../components/ErrorBox";
import PaginationBar from "../../components/PaginationBar";

const VERDICT_LABEL: Record<AuditVerdict["verdict"], string> = {
  corroborated: "corroborated",
  stale: "stale",
  contradicted: "contradicted",
  unfalsifiable: "unfalsifiable",
  duplicate_of: "duplicate of",
  conflicts_with: "conflicts with",
  ungrounded: "ungrounded",
};

const VERDICT_COLOR: Record<AuditVerdict["verdict"], string> = {
  corroborated: "green",
  stale: "yellow",
  contradicted: "red",
  unfalsifiable: "yellow",
  duplicate_of: "yellow",
  conflicts_with: "red",
  ungrounded: "red",
};

const ACTION_LABEL: Record<AuditVerdict["proposed_action"], string> = {
  none: "no action",
  archive: "archive",
  merge: "merge",
  flag_for_rewrite: "flag for rewrite",
  escalate_to_human: "escalate to human",
};

function needsManualResolution(v: AuditVerdict): boolean {
  return v.verdict === "conflicts_with" || v.proposed_action === "escalate_to_human";
}

export default function AuditQueue() {
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  // "no action" verdicts mean "this learning is fine" -- there is nothing to
  // decide, and approving one changes nothing. They outnumbered actionable
  // verdicts 3:1 and buried them, so they are hidden unless asked for.
  const [showNoAction, setShowNoAction] = useState(false);
  const { data, isPending, error, refetch } = useAuditVerdicts(
    "proposed",
    page,
    perPage,
    showNoAction,
  );
  const approve = useApproveVerdict();
  const reject = useRejectVerdict();

  const items = data?.items ?? [];

  return (
    <div>
      <div className="row flex-between" style={{ marginBottom: 10, flexWrap: "wrap", gap: 8 }}>
        <span className="muted" style={{ fontSize: 11.5 }}>
          {showNoAction
            ? "Showing all verdicts, including ones that need no decision."
            : "Showing verdicts that need a decision. “No action” verdicts are hidden."}
        </span>
        <label className="row" style={{ gap: 6, fontSize: 11.5 }}>
          <input
            type="checkbox"
            checked={showNoAction}
            onChange={(e) => {
              setShowNoAction(e.target.checked);
              setPage(1);
            }}
          />
          Show “no action” verdicts
        </label>
      </div>

      {isPending && <div className="spinner">Loading audit verdicts…</div>}
      {error && <ErrorBox message={error.message} onRetry={refetch} />}

      {data && items.length === 0 && (
        <div className="nw-card" style={{ padding: 24, textAlign: "center" }}>
          <p className="muted">
            {showNoAction
              ? "No proposed verdicts awaiting review."
              : "Nothing needs a decision. Tick “Show no action verdicts” to see what the auditor checked and left alone."}
          </p>
        </div>
      )}

      {items.map((v) => {
        const manual = needsManualResolution(v);
        return (
          <div className="nw-card learning-card" key={v.id} style={{ marginBottom: 14 }}>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <b>{v.learning_topic ?? v.learning_id}</b>
              <span className={`badge ${VERDICT_COLOR[v.verdict]}`}>{VERDICT_LABEL[v.verdict]}</span>
              <span className="badge">{Math.round(v.confidence * 100)}% confidence</span>
              {manual && <span className="badge yellow">needs manual resolution</span>}
            </div>

            {/* Which repo was searched, and which repo the learning is about.
                Without both, "no matches found in the codebase" cannot be
                judged -- an audit of a Python backend once proposed archiving
                a React learning on exactly that reasoning. */}
            <div className="row muted" style={{ gap: 8, flexWrap: "wrap", fontSize: 12 }}>
              <span>
                audited against <code>{v.audited_repo_path ?? "unknown repo"}</code>
              </span>
              <span>·</span>
              {v.learning_repo_path ? (
                <span>
                  learning scoped to <code>{v.learning_repo_path}</code>
                </span>
              ) : (
                <span className="yellow">
                  global learning — one repo cannot settle it
                </span>
              )}
            </div>

            {v.rationale && (
              <div className="learning-hint">
                <ReactMarkdown>{v.rationale}</ReactMarkdown>
              </div>
            )}

            {manual && v.related_learning_id && (
              <div className="row" style={{ gap: 12, alignItems: "stretch", marginTop: 8 }}>
                <div className="nw-card" style={{ flex: 1, padding: 10 }}>
                  <div className="muted" style={{ fontSize: 11 }}>
                    This learning
                  </div>
                  <div className="mono" style={{ fontSize: 12 }}>
                    {v.learning_id}
                  </div>
                </div>
                <div className="nw-card" style={{ flex: 1, padding: 10 }}>
                  <div className="muted" style={{ fontSize: 11 }}>
                    Related learning
                  </div>
                  <div className="mono" style={{ fontSize: 12 }}>
                    {v.related_learning_id}
                  </div>
                </div>
              </div>
            )}

            {/* A rewrite is only judgeable next to what it replaces, and
                approving it must visibly change something -- these actions
                used to be silent no-ops. */}
            {v.suggested_hint_text && (
              <div className="row" style={{ gap: 12, alignItems: "stretch", marginTop: 10 }}>
                <div className="nw-card" style={{ flex: 1, padding: 10 }}>
                  <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>
                    Current wording
                  </div>
                  <div style={{ fontSize: 12.5 }}>{v.current_hint_text ?? "—"}</div>
                </div>
                <div className="nw-card" style={{ flex: 1, padding: 10 }}>
                  <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>
                    Proposed wording {v.proposed_action === "merge" ? "(for the survivor)" : ""}
                  </div>
                  <div style={{ fontSize: 12.5 }}>{v.suggested_hint_text}</div>
                </div>
              </div>
            )}

            {v.citations.length > 0 && (
              <div style={{ marginTop: 10 }}>
                <div className="muted" style={{ fontSize: 11, marginBottom: 6 }}>
                  Citations
                </div>
                {v.citations.map((c, i) => (
                  <div key={i} style={{ marginBottom: 8 }}>
                    <div className="fileref mono" style={{ fontSize: 11.5 }}>
                      {c.file}:{c.line}
                    </div>
                    <pre className="nw-card" style={{ padding: 8, marginTop: 4 }}>
                      {c.quote}
                    </pre>
                  </div>
                ))}
              </div>
            )}

            <div className="row flex-between" style={{ marginTop: 10 }}>
              <span className="muted" style={{ fontSize: 11.5 }}>
                Proposed action: {ACTION_LABEL[v.proposed_action]}
              </span>
              <div className="row" style={{ gap: 8 }}>
                {!manual && (
                  <button
                    className="btn-p"
                    disabled={approve.isPending}
                    onClick={() => approve.mutate([v.id])}
                  >
                    Approve
                  </button>
                )}
                <button
                  className="btn-o"
                  disabled={reject.isPending}
                  onClick={() => reject.mutate([v.id])}
                >
                  Reject
                </button>
              </div>
            </div>

            {manual && !v.related_learning_id && (
              <p className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>
                This verdict needs a human to resolve the conflict directly — no related learning is
                attached to compare side by side.
              </p>
            )}
          </div>
        );
      })}

      {data && (
        <PaginationBar
          page={data.page}
          perPage={data.per_page}
          total={data.total}
          onPageChange={setPage}
          onPerPageChange={(n) => {
            setPerPage(n);
            setPage(1);
          }}
        />
      )}
    </div>
  );
}
