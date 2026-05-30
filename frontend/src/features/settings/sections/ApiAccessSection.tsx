import ErrorBox from "../../../components/ErrorBox";
import EnvChip from "../../../components/EnvChip";
import { useSettings } from "../../../api/queries";

export default function ApiAccessSection() {
  const { data, error, isPending, refetch } = useSettings();

  if (isPending) return <div className="spinner" />;
  if (error) return <ErrorBox message={error.message} onRetry={refetch} />;

  return (
    <div className="nw-card set-card">
      <h3>API access</h3>
      <p className="muted">
        Every API request (and the WebSocket log stream) is gated behind a bearer token. Read-only —
        secrets are never editable or readable from the UI. Rotate by updating the env and
        restarting.
      </p>
      <div className="f-row">
        <div className="fl">
          <b>API token</b>
          <span>
            required as <code>Authorization: Bearer &lt;token&gt;</code>, or as the WS{" "}
            <code>?token=</code> query param
          </span>
        </div>
        <div>
          {data?.secrets.api_token ? (
            <EnvChip envVar="ARGUS_API_TOKEN" />
          ) : (
            <span className="muted">not configured</span>
          )}
        </div>
      </div>
    </div>
  );
}
