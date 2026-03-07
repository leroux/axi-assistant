import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import type { ChatMessage } from "../types/messages";

interface Props {
  messages: ChatMessage[];
  streamingText: string;
}

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function sourceTag(source?: string) {
  if (!source || source === "web") return null;
  return <span className="msg-source">{source}</span>;
}

export function ChatArea({ messages, streamingText }: Props) {
  const areaRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const userScrolled = useRef(false);

  // Debounced streaming text for markdown rendering perf
  const [debouncedStream, setDebouncedStream] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebouncedStream(streamingText), 80);
    return () => clearTimeout(t);
  }, [streamingText]);

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    userScrolled.current = false;
    setShowScrollBtn(false);
  }, []);

  // Auto-scroll only if user hasn't scrolled up
  useEffect(() => {
    if (!userScrolled.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, debouncedStream]);

  // Detect scroll position
  useEffect(() => {
    const area = areaRef.current;
    if (!area) return;
    const onScroll = () => {
      const atBottom = area.scrollHeight - area.scrollTop - area.clientHeight < 80;
      userScrolled.current = !atBottom;
      setShowScrollBtn(!atBottom);
    };
    area.addEventListener("scroll", onScroll, { passive: true });
    return () => area.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div className="chat-area" data-testid="chat-area" ref={areaRef}>
      {messages.map((msg) => (
        <div key={msg.id} className={`chat-message ${msg.role}`} data-testid={`msg-${msg.role}`}>
          <span className="msg-time">{formatTime(msg.ts)}</span>
          <span className="msg-role">
            {msg.role === "user"
              ? "You"
              : msg.role === "assistant"
                ? "Axi"
                : msg.role === "error"
                  ? "Error"
                  : "System"}
          </span>
          {sourceTag(msg.source)}
          <span className="msg-text">
            <ReactMarkdown rehypePlugins={[rehypeHighlight]}>{msg.text}</ReactMarkdown>
          </span>
        </div>
      ))}
      {debouncedStream && (
        <div className="chat-message assistant streaming" data-testid="streaming-indicator">
          <span className="msg-role">Axi</span>
          <span className="msg-text">
            <ReactMarkdown rehypePlugins={[rehypeHighlight]}>{debouncedStream}</ReactMarkdown>
          </span>
          <span className="cursor" />
        </div>
      )}
      <div ref={bottomRef} />
      {showScrollBtn && (
        <button className="scroll-to-bottom" onClick={scrollToBottom} data-testid="scroll-to-bottom">
          &darr;
        </button>
      )}
    </div>
  );
}
