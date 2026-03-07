import { useCallback, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";

interface Props {
  agent: string;
  plan: string;
  onApprove: (agent: string, feedback: string) => void;
  onReject: (agent: string, feedback: string) => void;
}

export function PlanApproval({ agent, plan, onApprove, onReject }: Props) {
  const [feedback, setFeedback] = useState("");

  const handleApprove = useCallback(() => {
    onApprove(agent, feedback);
  }, [agent, feedback, onApprove]);

  const handleReject = useCallback(() => {
    onReject(agent, feedback);
  }, [agent, feedback, onReject]);

  return (
    <div className="plan-approval" data-testid="plan-approval">
      <div className="plan-header">Plan Approval Required</div>
      <div className="plan-content" data-testid="plan-content">
        <ReactMarkdown rehypePlugins={[rehypeHighlight]}>{plan}</ReactMarkdown>
      </div>
      <textarea
        className="plan-feedback"
        placeholder="Optional feedback..."
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={2}
      />
      <div className="plan-actions">
        <button className="btn-approve" onClick={handleApprove}>
          Approve
        </button>
        <button className="btn-reject" onClick={handleReject}>
          Reject
        </button>
      </div>
    </div>
  );
}
