import type { ReviewCandidate } from "../api/types";

// The gate's decision, not severity: green kept, red dropped, grey unjudged.
function verdictClass(c: ReviewCandidate): string {
  if (c.valid === true) return "kept";
  if (c.valid === false) return "dropped";
  return "unjudged";
}

// Grounding rejects a finding whose evidence quote is absent from the source,
// which is worth naming separately from a verify rejection.
function verdictLabel(c: ReviewCandidate): string {
  if (c.valid === true) return "kept";
  if (c.valid === false) return c.gate === "grounding" ? "ungrounded" : "dropped";
  return "not judged";
}

export default function NodeFindings({ candidates }: { candidates: ReviewCandidate[] }) {
  if (candidates.length === 0) return null;
  const kept = candidates.filter((c) => c.valid === true).length;
  const dropped = candidates.filter((c) => c.valid === false).length;
  const unjudged = candidates.filter((c) => c.valid === null).length;

  return (
    <div className="nf-wrap">
      <div className="nf-head">
        <b>Candidate findings</b>
        <span className="muted">
          {candidates.length} produced
          {kept > 0 && <> · {kept} kept</>}
          {dropped > 0 && <> · {dropped} dropped</>}
          {unjudged > 0 && <> · {unjudged} not judged</>}
        </span>
      </div>
      <ul className="nf-list">
        {candidates.map((c) => (
          <li key={c.finding_id} className={`nf-item nf-${verdictClass(c)}`}>
            <div className="nf-row">
              <span className="nf-verdict">{verdictLabel(c)}</span>
              <span className="nf-title">{c.title ?? c.finding_id}</span>
              {c.severity && <span className="nf-sev">{c.severity}</span>}
            </div>
            {c.file_path && (
              <div className="nf-loc mono">
                {c.file_path}
                {c.line != null && `:${c.line}`}
              </div>
            )}
            {c.reason && <div className="nf-reason">{c.reason}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}
