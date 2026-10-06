import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMe } from "../../../api/queries";
import { api } from "../../../api/client";
import type { ModelEndpoint } from "../../../api/types";
import ErrorBox from "../../../components/ErrorBox";

const empty = {
  name: "github-gemini-free",
  provider: "gemini",
  model: "gemini/gemini-3.8-flash",
  api_key_ref: "GEMINI_API_KEY",
  base_url: null,
  is_default: false,
};

export default function EndpointsSection() {
  const qc = useQueryClient();
  const me = useMe();
  const isAdmin = me.data?.role === "admin";
  const rows = useQuery({ queryKey: ["model-endpoints"], queryFn: api.modelEndpoints });
  const [draft, setDraft] = useState<Omit<ModelEndpoint, "id" | "secret_present">>(empty);
  const [editing, setEditing] = useState<string>();
  const [probeMessage, setProbeMessage] = useState("");
  const save = useMutation({
    mutationFn: () => api.saveModelEndpoint(draft, editing),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["model-endpoints"] });
    },
  });
  const probe = useMutation({
    mutationFn: api.testModelEndpoint,
    onSuccess: (data) => setProbeMessage(data.response),
  });
  return (
    <div className="ui-card set-card">
      <h3>Model endpoints</h3>
      <p className="muted">
        GitHub imports use the configured repository endpoint. Supported options include Gemini,
        OpenRouter free models, and Vertex AI with Cloud billing credits.
      </p>
      {!isAdmin && <p className="muted">Sign in with an admin token to add, edit, or test endpoints.</p>}
      {rows.isPending && <p role="status">Loading endpoints…</p>}
      {rows.error && <ErrorBox message={rows.error.message} />}
      {(rows.data ?? []).map((row) => (
        <div className="f-row" key={row.id}>
          <div className="fl">
            <b>{row.name}</b>
            <span>
              {row.model} · {row.secret_present ? "credential present" : "credential missing"}
            </span>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <button
              className="btn-o"
              disabled={!isAdmin}
              onClick={() => {
                setEditing(row.id);
                setDraft(row);
                save.reset();
              }}
            >
              Edit
            </button>
            <button
              className="btn-o"
              disabled={!isAdmin || probe.isPending}
              onClick={() => {
                setProbeMessage("");
                probe.mutate(row.id);
              }}
            >
              {probe.isPending ? "Testing…" : "Test"}
            </button>
          </div>
        </div>
      ))}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        {(["name", "provider", "model", "api_key_ref"] as const).map((key) => (
          <div className="f-row" key={key}>
            <label htmlFor={`endpoint-${key}`}>{key.replaceAll("_", " ")}</label>
            <input
              id={`endpoint-${key}`}
              required
              value={draft[key] ?? ""}
              onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
            />
          </div>
        ))}
        <p className="muted">
          api key ref is the environment variable name. Credentials stay on the server.
        </p>
        <button className="btn-p" disabled={!isAdmin || save.isPending}>
          {save.isPending ? "Saving…" : editing ? "Save endpoint" : "Add endpoint"}
        </button>
        {editing && (
          <button
            className="btn-o"
            type="button"
            onClick={() => {
              setEditing(undefined);
              setDraft(empty);
            }}
          >
            New endpoint
          </button>
        )}
      </form>
      {save.error && <ErrorBox message={save.error.message} />}
      {probe.error && <ErrorBox message={probe.error.message} />}
      {save.isSuccess && <p role="status">Endpoint saved.</p>}
      {probeMessage && <p role="status">Model response: {probeMessage}</p>}
    </div>
  );
}
