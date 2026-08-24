import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import {
  getStatus,
  type ProgressState,
  type RunMetadata,
  type StatusResponse,
  type WsMessage,
} from "../api/client";

// Fallback cadence while the websocket is down - identical to the interval
// Dashboard.tsx used to poll on its own before this provider existed.
const POLL_INTERVAL_MS = 15_000;
// Reconnect backoff: starts fast (a dropped connection should recover
// quickly), doubles each failed attempt, caps at the same ceiling the
// backend's own MQTT reconnect loop uses (notifications/mqtt_sink.py).
const INITIAL_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 15_000;

export type ConnectionState = "connecting" | "open" | "reconnecting";

interface SyncStatusValue {
  status: StatusResponse | null;
  progress: ProgressState | null;
  connectionState: ConnectionState;
  error: string | null;
}

const SyncStatusContext = createContext<SyncStatusValue | null>(null);

export function useSyncStatus(): SyncStatusValue {
  const ctx = useContext(SyncStatusContext);
  if (ctx === null) {
    throw new Error("useSyncStatus must be used within a SyncStatusProvider");
  }
  return ctx;
}

function wsUrl(): string {
  // Same-origin, relative construction - no base-URL/env-var pattern exists
  // anywhere else in this app (see api/client.ts), production serves the
  // built SPA from the same FastAPI app that exposes /api/ws.
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/ws`;
}

export function SyncStatusProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [progress, setProgress] = useState<ProgressState | null>(null);
  const [connectionState, setConnectionState] =
    useState<ConnectionState>("connecting");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;
    let backoffMs = INITIAL_BACKOFF_MS;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    async function pollOnce() {
      try {
        const data = await getStatus();
        if (!cancelled) {
          setStatus(data);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load status");
        }
      }
    }

    function startPolling() {
      if (pollTimer !== null) return;
      pollOnce();
      pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
    }

    function stopPolling() {
      if (pollTimer !== null) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function handleMessage(event: MessageEvent) {
      let message: WsMessage;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      switch (message.type) {
        case "status_snapshot":
          setStatus(message.data as StatusResponse);
          setProgress(null);
          break;
        case "status":
          setStatus((prev) =>
            prev
              ? { ...prev, run_metadata: (message.data as { run_metadata: RunMetadata }).run_metadata }
              : prev,
          );
          break;
        case "sync_state":
          if ((message.data as { value: string }).value === "idle") {
            setProgress(null);
          }
          break;
        case "cycle_progress":
          setProgress(message.data as ProgressState);
          break;
        default:
          // "ping" (liveness only) and anything else (availability,
          // state_value - MQTT/Home Assistant concerns) are not consumed
          // by this dashboard.
          break;
      }
    }

    function connect() {
      if (cancelled) return;
      setConnectionState((prev) => (prev === "open" ? prev : "connecting"));
      const socket = new WebSocket(wsUrl());
      ws = socket;

      socket.onopen = () => {
        if (cancelled) {
          // Cleanup already ran while this socket was still CONNECTING -
          // it's now safe to close (open sockets close cleanly, with no
          // console warning, unlike closing one mid-handshake below).
          socket.close();
          return;
        }
        backoffMs = INITIAL_BACKOFF_MS;
        setConnectionState("open");
        setError(null);
        stopPolling();
      };
      socket.onmessage = handleMessage;
      socket.onerror = () => {
        // Not the reconnect trigger - onclose fires exactly once per
        // connection attempt and is what schedules the retry below;
        // onerror's firing guarantees are looser (it can precede onclose
        // by an arbitrary amount, or not fire at all), so scheduling from
        // both would risk double-scheduling a reconnect.
        if (!cancelled) setError("Live connection error");
      };
      socket.onclose = () => {
        if (cancelled) return;
        setConnectionState("reconnecting");
        startPolling();
        reconnectTimer = setTimeout(connect, backoffMs);
        backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
      };
    }

    // Start polling immediately, before the first connection attempt even
    // resolves, so there is never a gap with no data flowing at all - and
    // stop the instant the socket opens (see connect()'s onopen above).
    startPolling();
    connect();

    return () => {
      cancelled = true;
      stopPolling();
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      if (ws !== null) {
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        if (ws.readyState === WebSocket.CONNECTING) {
          // Don't close a socket that hasn't finished connecting yet -
          // Chrome logs a "WebSocket is closed before the connection is
          // established" console warning if you do (harmless, but noisy;
          // React StrictMode's mount->cleanup->mount-again dev behavior
          // hits this on every page load otherwise). Leave onopen wired -
          // it checks `cancelled` itself and closes cleanly once the
          // handshake actually finishes, see connect() above.
        } else {
          ws.onopen = null;
          ws.close();
        }
      }
    };
  }, []);

  return (
    <SyncStatusContext.Provider
      value={{ status, progress, connectionState, error }}
    >
      {children}
    </SyncStatusContext.Provider>
  );
}
