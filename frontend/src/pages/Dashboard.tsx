import { useEffect, useState } from "react";
import {
  ApiError,
  getSettings,
  setPaused,
  syncNow,
  updateSettings,
  type LogLevel,
  type ProgressState,
  type StatusResponse,
} from "../api/client";
import { useSyncStatus } from "../contexts/SyncStatusContext";
import { formatDateTime, relativeTime } from "../lib/format";

type SyncNowState = "idle" | "loading" | "requested" | "error";

type OverallState = "syncing" | "error" | "auth_required" | "paused" | "healthy";

function overallState(status: StatusResponse, isSyncing: boolean): OverallState {
  // Syncing takes precedence: last_error/auth_required describe the
  // PREVIOUS run's outcome, and a fresh attempt already in progress right
  // now is the more relevant thing to show.
  if (isSyncing) return "syncing";
  const rm = status.run_metadata;
  if (rm.last_error) return "error";
  if (rm.auth_required) return "auth_required";
  if (rm.paused) return "paused";
  return "healthy";
}

const STATE_META: Record<
  OverallState,
  { label: string; dot: string; badge: string }
> = {
  syncing: {
    label: "Syncing",
    dot: "bg-sky-500",
    badge: "bg-sky-500/15 text-sky-300 ring-1 ring-inset ring-sky-500/30",
  },
  error: {
    label: "Error",
    dot: "bg-rose-500",
    badge: "bg-rose-500/15 text-rose-300 ring-1 ring-inset ring-rose-500/30",
  },
  auth_required: {
    label: "Auth required",
    dot: "bg-amber-500",
    badge:
      "bg-amber-500/15 text-amber-300 ring-1 ring-inset ring-amber-500/30",
  },
  paused: {
    label: "Paused",
    dot: "bg-slate-400",
    badge: "bg-slate-500/15 text-slate-300 ring-1 ring-inset ring-slate-500/30",
  },
  healthy: {
    label: "Healthy",
    dot: "bg-emerald-500",
    badge:
      "bg-emerald-500/15 text-emerald-300 ring-1 ring-inset ring-emerald-500/30",
  },
};

const LOG_LEVELS: LogLevel[] = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

function progressLabel(progress: ProgressState): string {
  if (progress.phase === "fetching") {
    const provider = typeof progress.provider === "string" ? progress.provider : "provider";
    return `Fetching from ${provider}`;
  }
  if (progress.phase === "submitting") {
    const budgetId = typeof progress.budget_id === "string" ? progress.budget_id : "";
    return budgetId ? `Submitting to YNAB (${budgetId})` : "Submitting to YNAB";
  }
  return progress.phase;
}

function StatTile({
  label,
  value,
  accent,
}: {
  label: string;
  value: number;
  accent?: "emerald" | "sky" | "amber" | "rose" | "slate";
}) {
  const accentClass =
    {
      emerald: "text-emerald-400",
      sky: "text-sky-400",
      amber: "text-amber-400",
      rose: "text-rose-400",
      slate: "text-slate-200",
    }[accent ?? "slate"] ?? "text-slate-200";

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
        {label}
      </p>
      <p className={`mt-2 text-2xl font-semibold tabular-nums ${accentClass}`}>
        {value.toLocaleString()}
      </p>
    </div>
  );
}

export default function Dashboard() {
  const { status, progress, connectionState, error } = useSyncStatus();
  const [syncNowState, setSyncNowState] = useState<SyncNowState>("idle");
  const [syncNowError, setSyncNowError] = useState<string | null>(null);
  const [logLevel, setLogLevel] = useState<LogLevel | null>(null);
  const [logLevelSaving, setLogLevelSaving] = useState(false);
  const [logLevelError, setLogLevelError] = useState<string | null>(null);
  const [pauseSaving, setPauseSaving] = useState(false);
  const [pauseError, setPauseError] = useState<string | null>(null);

  useEffect(() => {
    getSettings()
      .then((s) => setLogLevel(s.log_level))
      .catch(() => setLogLevelError("Failed to load current log level"));
  }, []);

  async function handleLogLevelChange(level: LogLevel) {
    const previous = logLevel;
    setLogLevel(level); // optimistic - reverted below on failure
    setLogLevelSaving(true);
    setLogLevelError(null);
    try {
      const updated = await updateSettings({ log_level: level });
      setLogLevel(updated.log_level);
    } catch (e) {
      setLogLevel(previous);
      setLogLevelError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Failed to update log level",
      );
    } finally {
      setLogLevelSaving(false);
    }
  }

  async function handleTogglePaused(nextPaused: boolean) {
    setPauseSaving(true);
    setPauseError(null);
    try {
      await setPaused(nextPaused);
    } catch (e) {
      setPauseError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Failed to update pause state",
      );
    } finally {
      setPauseSaving(false);
    }
  }

  async function handleSyncNow() {
    setSyncNowState("loading");
    setSyncNowError(null);
    try {
      await syncNow();
      setSyncNowState("requested");
      setTimeout(() => setSyncNowState("idle"), 5000);
    } catch (e) {
      const detail =
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Sync-now request failed";
      setSyncNowState("error");
      setSyncNowError(detail);
    }
  }

  if (error && !status) {
    return (
      <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-6 text-rose-200">
        <p className="font-semibold">Couldn't reach the sync service</p>
        <p className="mt-1 text-sm text-rose-300/80">{error}</p>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="flex items-center gap-3 text-slate-400">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-slate-300" />
        Loading status…
      </div>
    );
  }

  const rm = status.run_metadata;
  const state = overallState(status, progress !== null);
  const meta = STATE_META[state];

  return (
    <div className="space-y-6">
      {/* Header / overall state */}
      <div className="flex flex-col gap-4 rounded-xl border border-slate-800 bg-slate-900/60 p-6 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <span className="relative flex h-3 w-3">
            {(state === "healthy" || state === "syncing") && (
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
            )}
            <span
              className={`relative inline-flex h-3 w-3 rounded-full ${meta.dot}`}
            />
          </span>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-semibold text-slate-100">
                Sync status
              </h1>
              <span
                className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${meta.badge}`}
              >
                {meta.label}
              </span>
            </div>
            <p className="mt-0.5 text-sm text-slate-400">
              Last run {relativeTime(rm.last_run_at)} · last success{" "}
              {relativeTime(rm.last_success_at)}
            </p>
            {progress !== null && (
              <p className="mt-0.5 text-sm text-sky-300">
                {progressLabel(progress)}
              </p>
            )}
          </div>
        </div>
        <div className="flex flex-col items-start gap-2 sm:items-end">
          <div className="flex items-center gap-2">
            <button
              onClick={handleSyncNow}
              disabled={syncNowState === "loading"}
              className="inline-flex items-center gap-1.5 rounded-lg bg-sky-500/15 px-3 py-1.5 text-xs font-semibold text-sky-300 ring-1 ring-inset ring-sky-500/30 hover:bg-sky-500/25 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {syncNowState === "loading" && (
                <span className="h-3 w-3 animate-spin rounded-full border-2 border-sky-300/40 border-t-sky-300" />
              )}
              {syncNowState === "requested" ? "Sync requested" : "Sync now"}
            </button>
            {rm.paused ? (
              <button
                onClick={() => void handleTogglePaused(false)}
                disabled={pauseSaving}
                className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-300 ring-1 ring-inset ring-emerald-500/30 hover:bg-emerald-500/25 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {pauseSaving && (
                  <span className="h-3 w-3 animate-spin rounded-full border-2 border-emerald-300/40 border-t-emerald-300" />
                )}
                Resume syncing
              </button>
            ) : (
              <button
                onClick={() => void handleTogglePaused(true)}
                disabled={pauseSaving}
                className="inline-flex items-center gap-1.5 rounded-lg bg-amber-500/15 px-3 py-1.5 text-xs font-semibold text-amber-300 ring-1 ring-inset ring-amber-500/30 hover:bg-amber-500/25 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {pauseSaving && (
                  <span className="h-3 w-3 animate-spin rounded-full border-2 border-amber-300/40 border-t-amber-300" />
                )}
                Pause syncing
              </button>
            )}
          </div>
          {syncNowState === "error" && (
            <p className="max-w-[16rem] text-xs text-rose-400">
              {syncNowError}
            </p>
          )}
          {pauseError && (
            <p className="max-w-[16rem] text-xs text-rose-400">
              {pauseError}
            </p>
          )}
          <div className="text-left sm:text-right">
            <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Next scheduled run
            </p>
            <p className="mt-0.5 text-sm font-medium text-slate-200">
              {formatDateTime(status.next_fire_at)}
            </p>
            <p className="text-xs text-slate-500">
              {relativeTime(status.next_fire_at)}
            </p>
          </div>
        </div>
      </div>

      {rm.last_error && (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4">
          <p className="text-sm font-semibold text-rose-300">
            Last run failed
          </p>
          <p className="mt-1 break-words font-mono text-xs text-rose-200/90">
            {rm.last_error}
          </p>
        </div>
      )}

      {rm.auth_required && !rm.last_error && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">
          Re-authentication with SpareBank1 is required before syncing can
          continue.
        </div>
      )}

      {/* Stat tiles */}
      <div>
        <h2 className="mb-3 text-sm font-medium text-slate-400">
          Last run
        </h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <StatTile
            label="Fetched"
            value={rm.fetched_last_run}
            accent="slate"
          />
          <StatTile
            label="Imported"
            value={rm.imported_last_run}
            accent="emerald"
          />
          <StatTile label="Updated" value={rm.updated_last_run} accent="sky" />
          <StatTile
            label="Duplicates"
            value={rm.duplicates_last_run}
            accent="amber"
          />
          <StatTile
            label="Resolved deleted"
            value={rm.resolved_deleted_last_run}
            accent="rose"
          />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Imported (all time)
          </p>
          <p className="mt-2 text-3xl font-semibold tabular-nums text-slate-100">
            {rm.imported_total.toLocaleString()}
          </p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Schedule
          </p>
          <p className="mt-2 font-mono text-sm text-slate-200">
            {status.cron_expression}
          </p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Log level
          </p>
          <select
            value={logLevel ?? ""}
            disabled={logLevel === null || logLevelSaving}
            onChange={(e) => void handleLogLevelChange(e.target.value as LogLevel)}
            className="mt-2 w-full rounded-lg border border-slate-700 bg-slate-900/60 px-2.5 py-1.5 text-sm text-slate-200 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {logLevel === null && <option value="">Loading…</option>}
            {LOG_LEVELS.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
          {logLevelError && (
            <p className="mt-1.5 text-xs text-rose-400">{logLevelError}</p>
          )}
        </div>
      </div>

      {/* Accounts */}
      <div>
        <h2 className="mb-3 text-sm font-medium text-slate-400">
          Configured accounts
        </h2>
        <div className="overflow-hidden rounded-xl border border-slate-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-900/80 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2.5 font-medium">Account</th>
                <th className="px-4 py-2.5 font-medium">Key</th>
                <th className="px-4 py-2.5 font-medium">YNAB budget</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800 bg-slate-900/40">
              {status.accounts.map((a) => (
                <tr key={a.key}>
                  <td className="px-4 py-2.5 font-medium text-slate-200">
                    {a.display_name}
                  </td>
                  <td className="px-4 py-2.5 font-mono text-xs text-slate-400">
                    {a.key}
                  </td>
                  <td className="px-4 py-2.5 text-slate-300">
                    {a.ynab_budget}
                  </td>
                </tr>
              ))}
              {status.accounts.length === 0 && (
                <tr>
                  <td
                    className="px-4 py-6 text-center text-slate-500"
                    colSpan={3}
                  >
                    No accounts configured.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="flex items-center justify-between text-xs text-slate-600">
        <span className="flex items-center gap-3">
          <span>{status.version} — built {formatDateTime(status.build_timestamp === "unknown" ? null : status.build_timestamp)}</span>
          <span>{error ?? ""}</span>
        </span>
        {connectionState === "open" ? (
          <span className="flex items-center gap-1.5 text-emerald-400">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
            Live
          </span>
        ) : (
          // Deliberately a visible badge, not a quiet footer note - a
          // broken reverse-proxy websocket upgrade fails silently
          // otherwise (the dashboard would just keep working via the 15s
          // poll below, with nothing to prompt anyone to go check).
          <span className="flex items-center gap-1.5 rounded-full bg-amber-500/15 px-2 py-1 font-medium text-amber-300 ring-1 ring-inset ring-amber-500/30">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />
            Reconnecting… showing data every 15s
          </span>
        )}
      </div>
    </div>
  );
}
