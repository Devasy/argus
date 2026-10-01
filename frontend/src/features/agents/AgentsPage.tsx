import { useParams } from "react-router-dom";
import { useState } from "react";
import PipelineRail from "./PipelineRail";
import AgentList from "./AgentList";
import AgentEditor from "./AgentEditor";
import VersionHistory from "./VersionHistory";
import TestOnMrDialog from "./TestOnMrDialog";
import { useAgents } from "../../api/queries";
import ErrorBox from "../../components/ErrorBox";

export default function AgentsPage() {
  const { agentId } = useParams();
  const agents = useAgents(1, 200);
  const [testingAgent, setTestingAgent] = useState<string | null>(null);

  const isCreating = agentId === "new";
  const selected = agentId && !isCreating
    ? agents.data?.items.find((a) => a.id === agentId)
    : undefined;
  const notFound = !!agentId && !isCreating && !agents.isPending && !agents.error && !selected;

  return (
    <div>
      <div className="page-head">
        <h1>Agents</h1>
      </div>
      <div className="agents-layout">
        <div>
          <PipelineRail />
          <AgentList />
        </div>
        <div>
          {!agentId && (
            <div className="nw-card" style={{ padding: 24, textAlign: "center" }}>
              <p className="muted">Select an agent to edit its guidelines, or add a new one.</p>
            </div>
          )}
          {agentId && agents.error && (
            <ErrorBox message={agents.error.message} onRetry={agents.refetch} />
          )}
          {agentId && !isCreating && !agents.error && agents.isPending && (
            <div className="nw-card" style={{ padding: 24, textAlign: "center" }}>
              <p className="muted">Loading agent…</p>
            </div>
          )}
          {notFound && (
            <div className="nw-card" style={{ padding: 24, textAlign: "center" }}>
              <p className="muted">Agent not found.</p>
            </div>
          )}
          {agentId && !agents.error && (isCreating || selected) && (
            <AgentEditor
              agent={isCreating ? null : (selected ?? null)}
              onTestOnMr={(name) => setTestingAgent(name)}
            />
          )}
          {agentId && !isCreating && selected && <VersionHistory agentId={selected.id} />}
        </div>
      </div>
      {testingAgent && (
        <TestOnMrDialog agentName={testingAgent} onClose={() => setTestingAgent(null)} />
      )}
    </div>
  );
}
