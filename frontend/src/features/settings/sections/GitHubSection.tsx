import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import ErrorBox from "../../../components/ErrorBox";

export default function GitHubSection() {
  const status = useQuery({
    queryKey: ["github-integration"],
    queryFn: api.githubStatus,
    retry: false,
  });
  return (
    <div className="ui-card set-card">
      <h3>GitHub connection</h3>
      <p>
        Configure ARGUS_GITHUB_TOKEN in the server environment, or use a GitHub App installation and
        private key file.
      </p>
      <p className="muted">
        A PAT acts as its account. A GitHub App gives the official app[bot] identity. Public PR
        imports use polling and need no webhook.
      </p>
      {status.isPending && <p role="status">Checking connection…</p>}
      {status.error && <ErrorBox message={status.error.message} />}
      {status.data && (
        <p role="status">
          {status.data.username
            ? `Connected as ${status.data.username}`
            : (status.data.error ?? "Credentials not configured")}
        </p>
      )}
      <button className="btn-o" disabled={status.isFetching} onClick={() => void status.refetch()}>
        Check connection
      </button>
    </div>
  );
}
