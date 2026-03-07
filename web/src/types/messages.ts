/** Server -> Client messages */
export type ServerMessage =
  | AgentListMessage
  | AssistantMessage
  | SystemMessage
  | BroadcastMessage
  | UserEchoMessage
  | StreamEventMessage
  | AgentLifecycleMessage
  | PlanRequestMessage
  | QuestionMessage
  | TodoUpdateMessage
  | ErrorMessage
  | PongMessage
  | LogEventMessage
  | ChatHistoryMessage;

export interface AgentListMessage {
  type: "agent_list";
  agents: AgentInfo[];
}

export interface AgentInfo {
  name: string;
  agent_type: string;
  awake: boolean;
  activity_phase: string;
  session_id: string | null;
}

export interface AssistantMessage {
  type: "assistant_message";
  agent: string;
  text: string;
  ts: number;
}

export interface SystemMessage {
  type: "system";
  agent?: string;
  text: string;
  ts: number;
}

export interface BroadcastMessage {
  type: "broadcast";
  text: string;
  ts: number;
}

export interface UserEchoMessage {
  type: "user_message";
  agent: string;
  content: string;
  source: string;
  ts: number;
}

export interface StreamEventMessage {
  type: "stream_event";
  event_type: string;
  agent: string;
  data: Record<string, unknown>;
  ts: number;
}

export interface AgentLifecycleMessage {
  type:
    | "agent_wake"
    | "agent_sleep"
    | "agent_spawn"
    | "agent_kill"
    | "agent_reconnect"
    | "session_id"
    | "idle_reminder";
  agent: string;
  ts: number;
  [key: string]: unknown;
}

export interface PlanRequestMessage {
  type: "plan_request";
  agent: string;
  plan: string;
  ts: number;
}

export interface QuestionData {
  question: string;
  header?: string;
  options: QuestionOption[];
  multiSelect?: boolean;
}

export interface QuestionOption {
  label: string;
  description?: string;
}

export interface QuestionMessage {
  type: "question";
  agent: string;
  questions: QuestionData[];
  ts: number;
}

export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
  activeForm?: string;
}

export interface TodoUpdateMessage {
  type: "todo_update";
  agent: string;
  todos: TodoItem[];
  ts: number;
}

export interface ErrorMessage {
  type: "error";
  agent?: string;
  text: string;
}

export interface PongMessage {
  type: "pong";
  ts: number;
}

export interface LogEventMessage {
  type: "log_event";
  event: Record<string, unknown>;
}

export interface ChatHistoryMessage {
  type: "chat_history";
  history: Record<string, Array<{
    role: string;
    text: string;
    ts: number;
    source?: string;
    stream_event_type?: string;
  }>>;
}

/** Client -> Server messages */
export type ClientMessage =
  | { type: "message"; agent: string; content: string }
  | {
      type: "plan_approval";
      agent: string;
      approved: boolean;
      feedback: string;
    }
  | { type: "question_answer"; agent: string; answers: Record<string, string> }
  | { type: "subscribe"; agents: string[] }
  | { type: "ping" };

/** Chat message for display */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system" | "error";
  text: string;
  agent: string;
  ts: number;
  source?: string;
  streamEventType?: string;
}
