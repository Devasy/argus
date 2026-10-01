import { CheckCircle, ExternalLink } from "lucide-react";
import ReactMarkdown from "react-markdown";
import type { CodeEvent, Thread, ThreadNote } from "../api/types";
import { threadEntries } from "../lib/threadEntries";
import StatusBadge from "./StatusBadge";

const fmtTime = (iso: string | null) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";

const fmtDateTime = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "");

const AUTHOR_LABEL: Record<string, string> = {
  bot: "argus",
  human: "human",
  external_bot: "other bot",
};

const VERDICT_CLASS: Record<string, string> = {
  accepted: "green",
  rejected: "red",
  acknowledged: "yellow",
  question: "yellow",
  unclear: "yellow",
};

function NoteRow({ note }: { note: ThreadNote }) {
  return (
    <div className={`thread-note${note.depth > 0 ? " reply" : ""}`}>
      <div className="row" style={{ gap: 8 }}>
        <strong>{note.author_username ?? "unknown"}</strong>
        <span className="badge" data-kind={note.author_type}>
          {AUTHOR_LABEL[note.author_type] ?? note.author_type}
        </span>
        <span className="muted" title={fmtDateTime(note.created_at)}>
          {fmtTime(note.created_at)}
        </span>
      </div>
      <div className="md-body">
        <ReactMarkdown>{note.body}</ReactMarkdown>
      </div>
    </div>
  );
}

function EventRow({ event }: { event: CodeEvent }) {
  return (
    <div className="thread-event muted" title={fmtDateTime(event.at)}>
      <span aria-hidden="true">{event.kind === "commit" ? "⬆" : "✎"}</span>
      <span>{event.text}</span>
      <span>· {fmtTime(event.at)}</span>
    </div>
  );
}

export default function ThreadCard({ thread }: { thread: Thread }) {
  return (
    <div className="ui-card thread-card">
      <div className="flex-between thread-head">
        <div className="row" style={{ gap: 8 }}>
          <span className="mono">{thread.anchor}</span>
          {thread.resolved && (
            <span className="badge green">
              <CheckCircle size={12} aria-hidden="true" /> resolved
            </span>
          )}
          {thread.disposition && <StatusBadge status="done" label={thread.disposition} />}
          {thread.verdict && (
            <span className={`badge ${VERDICT_CLASS[thread.verdict] ?? ""}`}>
              AI verdict: {thread.verdict}
            </span>
          )}
        </div>
        {thread.gitlab_url && (
          <a href={thread.gitlab_url} target="_blank" rel="noreferrer">
            open in GitLab <ExternalLink size={12} />
          </a>
        )}
      </div>
      {thread.verdict_reason && <div className="muted thread-reason">{thread.verdict_reason}</div>}
      {threadEntries(thread).map((e) =>
        "note" in e ? (
          <NoteRow key={e.note.id} note={e.note} />
        ) : (
          <EventRow key={`${e.event.kind}-${e.event.at}-${e.event.text}`} event={e.event} />
        ),
      )}
    </div>
  );
}
