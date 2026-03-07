import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";

interface Props {
  onSend: (text: string) => void;
  disabled: boolean;
  loading?: boolean;
}

export function MessageInput({ onSend, disabled, loading }: Props) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setText("");
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }, [text, onSend]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit],
  );

  // Auto-grow textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 150) + "px";
  }, [text]);

  return (
    <div className="message-input" data-testid="message-input">
      {loading && <div className="loading-indicator" data-testid="loading-indicator">Processing...</div>}
      <textarea
        ref={textareaRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={disabled ? "Connecting..." : "Type a message..."}
        disabled={disabled}
        rows={1}
      />
      <button onClick={handleSubmit} disabled={disabled || !text.trim()}>
        Send
      </button>
    </div>
  );
}
