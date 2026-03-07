interface Props {
  agent: string;
  status: "connecting" | "connected" | "disconnected";
  showSystem: boolean;
  onToggleSystem: () => void;
}

export function StatusBar({ agent, status, showSystem, onToggleSystem }: Props) {
  const statusLabel =
    status === "connected"
      ? "Connected"
      : status === "connecting"
        ? "Connecting..."
        : "Disconnected";

  return (
    <div className="status-bar">
      <span className="active-agent" data-testid="active-agent-name">{agent || "No agent selected"}</span>
      <div className="status-bar-right">
        <button
          className={`toggle-system ${showSystem ? "active" : ""}`}
          onClick={onToggleSystem}
          title={showSystem ? "Hide system messages" : "Show system messages"}
          data-testid="toggle-system"
        >
          {showSystem ? "Sys" : "Sys"}
        </button>
        <span className={`connection-status ${status}`} data-testid="connection-status">{statusLabel}</span>
      </div>
    </div>
  );
}
