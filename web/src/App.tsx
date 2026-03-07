import { useCallback, useEffect, useRef, useState } from "react";
import { useTabNotification } from "./hooks/useTabNotification";
import { AgentList } from "./components/AgentList";
import { ChatArea } from "./components/ChatArea";
import { MessageInput } from "./components/MessageInput";
import { PlanApproval } from "./components/PlanApproval";
import { QuestionForm } from "./components/QuestionForm";
import { StatusBar } from "./components/StatusBar";
import { TodoList } from "./components/TodoList";
import { useWebSocket } from "./hooks/useWebSocket";
import type {
  AgentInfo,
  ChatMessage,
  QuestionData,
  ServerMessage,
  TodoItem,
} from "./types/messages";

const WS_URL = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;

let msgCounter = 0;
function nextId(): string {
  return `msg-${++msgCounter}-${Date.now()}`;
}

interface PendingPlan {
  agent: string;
  plan: string;
}

interface PendingQuestion {
  agent: string;
  questions: QuestionData[];
}

export default function App() {
  const { status, send, lastMessage } = useWebSocket(WS_URL);
  const notify = useTabNotification();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [activeAgent, setActiveAgent] = useState<string>("");
  const [messages, setMessages] = useState<Map<string, ChatMessage[]>>(
    new Map(),
  );
  const [streamingText, setStreamingText] = useState<Map<string, string>>(
    new Map(),
  );
  const [todos, setTodos] = useState<Map<string, TodoItem[]>>(new Map());
  const [pendingPlan, setPendingPlan] = useState<PendingPlan | null>(null);
  const [pendingQuestion, setPendingQuestion] =
    useState<PendingQuestion | null>(null);
  const [showSystem, setShowSystem] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [waitingFor, setWaitingFor] = useState<Set<string>>(new Set());
  const processedRef = useRef<ServerMessage | null>(null);

  // Periodically refresh agent list to keep activity_phase current
  useEffect(() => {
    if (status !== "connected") return;
    const id = setInterval(async () => {
      try {
        const res = await fetch("/api/agents");
        if (res.ok) {
          const fresh: AgentInfo[] = await res.json();
          setAgents(fresh);
        }
      } catch { /* ignore */ }
    }, 10_000);
    return () => clearInterval(id);
  }, [status]);

  const addMessage = useCallback((chatMsg: ChatMessage) => {
    setMessages((prev) => {
      const next = new Map(prev);
      const agentMsgs = next.get(chatMsg.agent) ?? [];
      next.set(chatMsg.agent, [...agentMsgs, chatMsg]);
      return next;
    });
  }, []);

  useEffect(() => {
    if (!lastMessage || lastMessage === processedRef.current) return;
    processedRef.current = lastMessage;

    const msg = lastMessage;

    // Clear loading indicator when we get a response for an agent
    const responseAgent = (msg as Record<string, unknown>).agent as string | undefined;
    if (responseAgent && (msg.type === "system" || msg.type === "assistant_message" || msg.type === "stream_event")) {
      setWaitingFor((prev) => {
        if (!prev.has(responseAgent)) return prev;
        const next = new Set(prev);
        next.delete(responseAgent);
        return next;
      });
    }

    switch (msg.type) {
      case "agent_list":
        setAgents(msg.agents);
        if (!activeAgent && msg.agents.length > 0) {
          setActiveAgent(msg.agents[0].name);
        }
        break;

      case "chat_history": {
        // Restore persisted messages on connect
        const history = (msg as unknown as { history: Record<string, Array<{ role: string; text: string; ts: number; source?: string; stream_event_type?: string }>> }).history;
        setMessages((prev) => {
          const next = new Map(prev);
          for (const [agent, msgs] of Object.entries(history)) {
            const restored: ChatMessage[] = msgs.map((m) => ({
              id: nextId(),
              role: m.role as ChatMessage["role"],
              text: m.text,
              agent,
              ts: m.ts,
              source: m.source,
              streamEventType: m.stream_event_type,
            }));
            next.set(agent, restored);
          }
          return next;
        });
        break;
      }

      case "assistant_message":
        addMessage({
          id: nextId(),
          role: "assistant",
          text: msg.text,
          agent: msg.agent,
          ts: msg.ts,
        });
        notify();
        break;

      case "system":
        addMessage({
          id: nextId(),
          role: "system",
          text: msg.text,
          agent: msg.agent ?? "",
          ts: msg.ts,
        });
        break;

      case "broadcast":
        addMessage({
          id: nextId(),
          role: "system",
          text: msg.text,
          agent: "",
          ts: Date.now() / 1000,
        });
        break;

      case "user_message":
        addMessage({
          id: nextId(),
          role: "user",
          text: msg.content,
          agent: msg.agent,
          ts: msg.ts,
          source: msg.source,
        });
        break;

      case "error":
        addMessage({
          id: nextId(),
          role: "error",
          text: msg.text,
          agent: msg.agent ?? "",
          ts: Date.now() / 1000,
        });
        break;

      case "stream_event":
        handleStreamEvent(msg.agent, msg.event_type, msg.data);
        break;

      case "agent_wake":
      case "agent_sleep":
      case "agent_spawn":
      case "agent_kill":
      case "agent_reconnect":
        handleLifecycleEvent(msg);
        break;

      case "todo_update":
        setTodos((prev) => {
          const next = new Map(prev);
          next.set(msg.agent, msg.todos);
          return next;
        });
        break;

      case "plan_request":
        setPendingPlan({ agent: msg.agent, plan: msg.plan });
        setActiveAgent(msg.agent);
        addMessage({
          id: nextId(),
          role: "system",
          text: "Plan approval requested — see panel below",
          agent: msg.agent,
          ts: msg.ts,
        });
        break;

      case "question":
        setPendingQuestion({ agent: msg.agent, questions: msg.questions });
        setActiveAgent(msg.agent);
        addMessage({
          id: nextId(),
          role: "system",
          text: "Question asked — see panel below",
          agent: msg.agent,
          ts: msg.ts,
        });
        break;

      case "session_id":
        setAgents((prev) =>
          prev.map((a) =>
            a.name === msg.agent ? { ...a, session_id: (msg as Record<string, unknown>).session_id as string } : a,
          ),
        );
        break;

      case "idle_reminder":
        addMessage({
          id: nextId(),
          role: "system",
          text: `Agent idle for ${Math.round((msg as Record<string, unknown>).idle_minutes as number)}m`,
          agent: msg.agent,
          ts: msg.ts,
          streamEventType: "lifecycle",
        });
        break;
    }
  }, [lastMessage, activeAgent, addMessage, notify]);

  function handleStreamEvent(
    agent: string,
    eventType: string,
    data: Record<string, unknown>,
  ) {
    switch (eventType) {
      case "TextDelta":
        setStreamingText((prev) => {
          const next = new Map(prev);
          next.set(agent, (prev.get(agent) ?? "") + (data.text as string));
          return next;
        });
        break;

      case "TextFlush":
        setStreamingText((prev) => {
          const next = new Map(prev);
          next.delete(agent);
          return next;
        });
        if (data.text) {
          addMessage({
            id: nextId(),
            role: "assistant",
            text: data.text as string,
            agent,
            ts: Date.now() / 1000,
          });
          notify();
        }
        break;

      case "ThinkingStart":
        addMessage({
          id: nextId(),
          role: "system",
          text: "Thinking...",
          agent,
          ts: Date.now() / 1000,
          streamEventType: "thinking",
        });
        break;

      case "ToolUseStart":
        addMessage({
          id: nextId(),
          role: "system",
          text: `Using tool: ${data.tool_name}`,
          agent,
          ts: Date.now() / 1000,
          streamEventType: "tool",
        });
        break;

      case "ToolUseEnd":
        if (data.preview) {
          addMessage({
            id: nextId(),
            role: "system",
            text: `${data.tool_name}: ${data.preview}`,
            agent,
            ts: Date.now() / 1000,
            streamEventType: "tool",
          });
        }
        break;

      case "StreamEnd":
        setStreamingText((prev) => {
          const next = new Map(prev);
          next.delete(agent);
          return next;
        });
        if (data.elapsed_s) {
          addMessage({
            id: nextId(),
            role: "system",
            text: `Completed in ${(data.elapsed_s as number).toFixed(1)}s`,
            agent,
            ts: Date.now() / 1000,
            streamEventType: "timing",
          });
        }
        break;
    }
  }

  function handleLifecycleEvent(msg: ServerMessage & { agent: string }) {
    const labels: Record<string, string> = {
      agent_wake: "woke up",
      agent_sleep: "went to sleep",
      agent_spawn: "spawned",
      agent_kill: "killed",
      agent_reconnect: "reconnected",
    };
    const label = labels[msg.type] ?? msg.type;
    addMessage({
      id: nextId(),
      role: "system",
      text: `Agent **${msg.agent}** ${label}`,
      agent: msg.agent,
      ts: (msg as unknown as { ts: number }).ts,
    });

    setAgents((prev) => {
      switch (msg.type) {
        case "agent_wake":
        case "agent_reconnect":
          return prev.map((a) =>
            a.name === msg.agent ? { ...a, awake: true } : a,
          );
        case "agent_sleep":
          return prev.map((a) =>
            a.name === msg.agent ? { ...a, awake: false } : a,
          );
        case "agent_kill":
          return prev.filter((a) => a.name !== msg.agent);
        case "agent_spawn":
          if (!prev.find((a) => a.name === msg.agent)) {
            return [
              ...prev,
              {
                name: msg.agent,
                agent_type: "claude_code",
                awake: true,
                activity_phase: "idle",
                session_id: null,
              },
            ];
          }
          return prev;
        default:
          return prev;
      }
    });
  }

  const handleSend = useCallback(
    (text: string) => {
      if (!activeAgent) return;
      send({ type: "message", agent: activeAgent, content: text });
      setWaitingFor((prev) => new Set(prev).add(activeAgent));
    },
    [activeAgent, send],
  );

  const handlePlanApprove = useCallback(
    (agent: string, feedback: string) => {
      send({ type: "plan_approval", agent, approved: true, feedback });
      setPendingPlan(null);
      addMessage({
        id: nextId(),
        role: "system",
        text: "Plan approved",
        agent,
        ts: Date.now() / 1000,
      });
    },
    [send, addMessage],
  );

  const handlePlanReject = useCallback(
    (agent: string, feedback: string) => {
      send({ type: "plan_approval", agent, approved: false, feedback });
      setPendingPlan(null);
      addMessage({
        id: nextId(),
        role: "system",
        text: `Plan rejected${feedback ? `: ${feedback}` : ""}`,
        agent,
        ts: Date.now() / 1000,
      });
    },
    [send, addMessage],
  );

  const handleQuestionSubmit = useCallback(
    (agent: string, answers: Record<string, string>) => {
      send({ type: "question_answer", agent, answers });
      setPendingQuestion(null);
      addMessage({
        id: nextId(),
        role: "system",
        text: "Answers submitted",
        agent,
        ts: Date.now() / 1000,
      });
    },
    [send, addMessage],
  );

  const currentMessages = messages.get(activeAgent) ?? [];
  const currentStreaming = streamingText.get(activeAgent) ?? "";
  const currentTodos = todos.get(activeAgent) ?? [];
  const showPlan =
    pendingPlan?.agent === activeAgent ? pendingPlan : null;
  const showQuestion =
    pendingQuestion?.agent === activeAgent ? pendingQuestion : null;

  const handleAgentSelect = useCallback((name: string) => {
    setActiveAgent(name);
    setSidebarOpen(false); // Close sidebar on mobile after selection
  }, []);

  return (
    <div className="app" data-testid="app">
      <button className="sidebar-toggle" onClick={() => setSidebarOpen((s) => !s)} aria-label="Toggle sidebar">
        &#9776;
      </button>
      <div className={`sidebar-overlay ${sidebarOpen ? "open" : ""}`} onClick={() => setSidebarOpen(false)} />
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`} data-testid="sidebar">
        <div className="sidebar-header">Axi Web</div>
        <AgentList
          agents={agents}
          active={activeAgent}
          onSelect={handleAgentSelect}
        />
        <TodoList todos={currentTodos} />
      </aside>
      <main className="main">
        <StatusBar
          agent={activeAgent}
          status={status}
          showSystem={showSystem}
          onToggleSystem={() => setShowSystem((s) => !s)}
        />
        <ChatArea
          messages={showSystem ? currentMessages : currentMessages.filter((m) => m.role !== "system")}
          streamingText={currentStreaming}
        />
        {showPlan && (
          <PlanApproval
            agent={showPlan.agent}
            plan={showPlan.plan}
            onApprove={handlePlanApprove}
            onReject={handlePlanReject}
          />
        )}
        {showQuestion && (
          <QuestionForm
            agent={showQuestion.agent}
            questions={showQuestion.questions}
            onSubmit={handleQuestionSubmit}
          />
        )}
        <MessageInput onSend={handleSend} disabled={status !== "connected"} loading={waitingFor.has(activeAgent)} />
      </main>
    </div>
  );
}
