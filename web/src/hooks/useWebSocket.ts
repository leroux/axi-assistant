import { useCallback, useEffect, useRef, useState } from "react";
import type { ClientMessage, ServerMessage } from "../types/messages";

type ConnectionStatus = "connecting" | "connected" | "disconnected";

interface UseWebSocketResult {
  status: ConnectionStatus;
  send: (msg: ClientMessage) => void;
  lastMessage: ServerMessage | null;
}

const PING_INTERVAL = 30_000; // 30s keepalive
const PONG_TIMEOUT = 10_000;  // 10s to receive pong before assuming dead

export function useWebSocket(url: string): UseWebSocketResult {
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const [lastMessage, setLastMessage] = useState<ServerMessage | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const pingTimer = useRef<ReturnType<typeof setInterval>>(undefined);
  const pongTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const stopPing = useCallback(() => {
    clearInterval(pingTimer.current);
    clearTimeout(pongTimer.current);
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    setStatus("connecting");
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus("connected");
      // Expose for Playwright test injection (harmless — same WS the browser already manages)
      (window as unknown as Record<string, unknown>).__testWs = ws;

      // Start ping/keepalive
      pingTimer.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ping" }));
          // Set a pong timeout — if no pong, close and reconnect
          pongTimer.current = setTimeout(() => {
            ws.close();
          }, PONG_TIMEOUT);
        }
      }, PING_INTERVAL);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as ServerMessage;
        // Clear pong timeout on any message (pong or otherwise)
        clearTimeout(pongTimer.current);
        if (msg.type === "pong") return; // Don't propagate pong
        setLastMessage(msg);
      } catch {
        // ignore malformed messages
      }
    };

    ws.onclose = () => {
      setStatus("disconnected");
      wsRef.current = null;
      stopPing();
      // Auto-reconnect after 2s
      reconnectTimer.current = setTimeout(connect, 2000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [url, stopPing]);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      stopPing();
      wsRef.current?.close();
    };
  }, [connect, stopPing]);

  const send = useCallback((msg: ClientMessage) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  return { status, send, lastMessage };
}
