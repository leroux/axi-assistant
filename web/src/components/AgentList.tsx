import type { AgentInfo } from "../types/messages";

interface Props {
  agents: AgentInfo[];
  active: string;
  onSelect: (name: string) => void;
}

export function AgentList({ agents, active, onSelect }: Props) {
  return (
    <div className="agent-list" data-testid="agent-list">
      {agents.map((agent) => (
        <button
          key={agent.name}
          className={`agent-item ${active === agent.name ? "active" : ""}`}
          onClick={() => onSelect(agent.name)}
          data-testid={`agent-item-${agent.name}`}
        >
          <span className={`status-dot ${agent.awake ? "awake" : "asleep"}`} />
          <span className="agent-name">{agent.name}</span>
          {agent.activity_phase !== "idle" && (
            <span className="agent-activity">{agent.activity_phase}</span>
          )}
        </button>
      ))}
      {agents.length === 0 && (
        <div className="no-agents" data-testid="no-agents">No agents available</div>
      )}
    </div>
  );
}
