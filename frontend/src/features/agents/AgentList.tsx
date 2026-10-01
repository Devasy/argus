import { useState } from "react";
import { Plus } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useAgents } from "../../api/queries";
import ErrorBox from "../../components/ErrorBox";
import PaginationBar from "../../components/PaginationBar";

export default function AgentList() {
  const { agentId } = useParams();
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const agents = useAgents(page, perPage);

  if (agents.isPending) return <div className="spinner">Loading agents…</div>;
  if (agents.error) return <ErrorBox message={agents.error.message} onRetry={agents.refetch} />;

  return (
    <div className="nw-card" style={{ padding: 10 }}>
      {agents.data!.items.map((a) => (
        <Link
          key={a.id}
          to={`/agents/${a.id}`}
          className={`agent-item${a.id === agentId ? " sel" : ""}`}
        >
          <div>
            <b style={{ fontSize: 13 }}>{a.name}</b>
            {!a.enabled && (
              <span className="badge red" style={{ marginLeft: 6 }}>
                disabled
              </span>
            )}
            <br />
            <span className="muted" style={{ fontSize: 11.5 }}>
              {a.description || "no description"}
            </span>
          </div>
        </Link>
      ))}
      <Link to="/agents/new" className="agent-item" style={{ borderStyle: "dashed" }}>
        <div className="row">
          <Plus size={15} /> Add agent
        </div>
      </Link>
      <div style={{ marginTop: 8 }}>
        <PaginationBar
          page={agents.data!.page}
          perPage={agents.data!.per_page}
          total={agents.data!.total}
          onPageChange={setPage}
          onPerPageChange={(n) => {
            setPerPage(n);
            setPage(1);
          }}
        />
      </div>
    </div>
  );
}
