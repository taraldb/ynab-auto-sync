import { useEffect, useState } from "react";
import {
  getStatus,
  listAuditEvents,
  type Account,
  type AuditEvent,
  type AuditEventSortColumn,
  type AuditEventType,
  type AuditEventsResponse,
  type SortDir,
} from "../api/client";
import { formatDateTime, formatNok } from "../lib/format";

const PAGE_SIZE = 50;

const TYPE_BADGE: Record<AuditEventType, string> = {
  created: "bg-emerald-500/15 text-emerald-300 ring-1 ring-inset ring-emerald-500/30",
  updated: "bg-sky-500/15 text-sky-300 ring-1 ring-inset ring-sky-500/30",
  duplicate: "bg-amber-500/15 text-amber-300 ring-1 ring-inset ring-amber-500/30",
  skipped: "bg-rose-500/15 text-rose-300 ring-1 ring-inset ring-rose-500/30",
};

const TILE_ACCENT: Record<AuditEventType, string> = {
  created: "text-emerald-400",
  updated: "text-sky-400",
  duplicate: "text-amber-400",
  skipped: "text-rose-400",
};

const TILE_LABEL: Record<AuditEventType, string> = {
  created: "Created",
  updated: "Updated",
  duplicate: "Duplicate",
  skipped: "Skipped",
};

const CATEGORY_FILTERS: AuditEventType[] = [
  "created",
  "updated",
  "duplicate",
  "skipped",
];

interface ColumnDef {
  label: string;
  sortKey: AuditEventSortColumn | null;
  align?: "right";
}

const COLUMNS: ColumnDef[] = [
  { label: "Time", sortKey: "occurred_at" },
  { label: "Type", sortKey: "event_type" },
  { label: "Source", sortKey: "source" },
  { label: "Account", sortKey: "account_key" },
  { label: "Payee", sortKey: "payee_name" },
  { label: "Memo", sortKey: "memo" },
  { label: "Amount", sortKey: "amount_milliunits", align: "right" },
  { label: "Detail", sortKey: "detail" },
];

function StatTile({ type, value }: { type: AuditEventType; value: number }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
        {TILE_LABEL[type]}
      </p>
      <p
        className={`mt-2 text-2xl font-semibold tabular-nums ${TILE_ACCENT[type]}`}
      >
        {value.toLocaleString()}
      </p>
    </div>
  );
}

export default function AuditLog() {
  const [data, setData] = useState<AuditEventsResponse | null>(null);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [includeSkipped, setIncludeSkipped] = useState(false);
  const [eventType, setEventType] = useState<AuditEventType | null>(null);
  const [accountKey, setAccountKey] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<AuditEventSortColumn>("occurred_at");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [accounts, setAccounts] = useState<Account[]>([]);

  useEffect(() => {
    getStatus()
      .then((res) => setAccounts(res.accounts))
      .catch(() => {
        // Non-fatal - the account filter dropdown just falls back to
        // showing raw account keys with no display-name lookup.
      });
  }, []);

  useEffect(() => {
    load({ reset: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [includeSkipped, eventType, accountKey, sortBy, sortDir]);

  async function load({ reset }: { reset: boolean }) {
    try {
      const offset = reset ? 0 : events.length;
      const res = await listAuditEvents({
        eventType,
        accountKey,
        includeSkipped,
        sortBy,
        sortDir,
        limit: PAGE_SIZE,
        offset,
      });
      setData(res);
      setEvents(reset ? res.events : [...events, ...res.events]);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load");
    }
  }

  function toggleSkipped() {
    const next = !includeSkipped;
    setIncludeSkipped(next);
    // A "skipped"-only filter makes no sense once skipped rows are hidden
    // again - fall back to "all" rather than leaving a filter selected
    // that can never match anything.
    if (!next && eventType === "skipped") setEventType(null);
  }

  function handleSort(column: AuditEventSortColumn) {
    if (column === sortBy) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortBy(column);
      // Time defaults to most-recent-first; every other column reads more
      // naturally starting A-Z / smallest-first on a fresh sort.
      setSortDir(column === "occurred_at" ? "desc" : "asc");
    }
  }

  function accountLabel(key: string): string {
    return accounts.find((a) => a.key === key)?.display_name || key;
  }

  if (loadError && !data) {
    return (
      <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-6 text-rose-200">
        <p className="font-semibold">Couldn't load the audit log</p>
        <p className="mt-1 text-sm text-rose-300/80">{loadError}</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex items-center gap-3 text-slate-400">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-slate-300" />
        Loading…
      </div>
    );
  }

  const hasMore = events.length < data.total;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">Audit log</h1>
          <p className="mt-0.5 text-sm text-slate-400">
            Every transaction a sync cycle created, updated, or recognized as
            a duplicate — plus rows it had to skip, if you want to see those
            too.
          </p>
        </div>
        <button
          onClick={() => load({ reset: true })}
          className="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-sm font-medium text-slate-200 hover:bg-slate-700/60"
        >
          Refresh
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {CATEGORY_FILTERS.map((t) => (
          <StatTile key={t} type={t} value={data.counts[t]} />
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => setEventType(null)}
          className={`rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${
            eventType === null
              ? "bg-slate-100 text-slate-900 ring-slate-100"
              : "bg-slate-500/15 text-slate-300 ring-slate-500/30 hover:bg-slate-500/25"
          }`}
        >
          All
        </button>
        {CATEGORY_FILTERS.filter((t) => t !== "skipped" || includeSkipped).map(
          (t) => (
            <button
              key={t}
              onClick={() => setEventType(eventType === t ? null : t)}
              className={`rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${
                eventType === t
                  ? TYPE_BADGE[t]
                  : "bg-slate-500/15 text-slate-400 ring-slate-500/30 hover:bg-slate-500/25"
              }`}
            >
              {TILE_LABEL[t]}
            </button>
          ),
        )}

        <select
          value={accountKey ?? ""}
          onChange={(e) => setAccountKey(e.target.value || null)}
          className="rounded-lg border border-slate-700 bg-slate-900/60 px-2.5 py-1 text-xs text-slate-200 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
        >
          <option value="">All accounts</option>
          {accounts.map((a) => (
            <option key={a.key} value={a.key}>
              {a.display_name || a.key}
            </option>
          ))}
        </select>

        <span className="ml-auto" />

        <button
          onClick={toggleSkipped}
          className={`rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${
            includeSkipped
              ? "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30 hover:bg-emerald-500/25"
              : "bg-slate-500/15 text-slate-300 ring-slate-500/30 hover:bg-slate-500/25"
          }`}
        >
          {includeSkipped ? "Skipped: Shown" : "Skipped: Hidden"}
        </button>
      </div>

      {events.length === 0 ? (
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-10 text-center text-slate-500">
          Nothing here yet — no matching events.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-900/80 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                {COLUMNS.map((col) => (
                  <th
                    key={col.label}
                    onClick={col.sortKey ? () => handleSort(col.sortKey!) : undefined}
                    className={`px-4 py-2.5 font-medium ${col.align === "right" ? "text-right" : ""} ${
                      col.sortKey ? "cursor-pointer select-none hover:text-slate-300" : ""
                    }`}
                  >
                    <span
                      className={`inline-flex items-center gap-1 ${col.align === "right" ? "flex-row-reverse" : ""}`}
                    >
                      {col.label}
                      {col.sortKey === sortBy && (
                        <span className="text-slate-300">
                          {sortDir === "asc" ? "▲" : "▼"}
                        </span>
                      )}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800 bg-slate-900/40">
              {events.map((ev) => {
                const isNegative =
                  ev.amount_milliunits !== null && ev.amount_milliunits < 0;
                return (
                  <tr key={ev.id}>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-300">
                      {formatDateTime(ev.occurred_at)}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${TYPE_BADGE[ev.event_type]}`}
                      >
                        {ev.event_type}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-400">
                      {ev.source}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-400">
                      {ev.account_key ? (
                        accountLabel(ev.account_key)
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3 font-medium text-slate-200">
                      {ev.payee_name ?? <span className="text-slate-500">—</span>}
                    </td>
                    <td className="max-w-xs truncate px-4 py-3 text-slate-400">
                      {ev.memo ?? <span className="text-slate-600">—</span>}
                    </td>
                    <td
                      className={`whitespace-nowrap px-4 py-3 text-right font-mono tabular-nums ${
                        ev.amount_milliunits === null
                          ? "text-slate-600"
                          : isNegative
                            ? "text-rose-300"
                            : "text-emerald-300"
                      }`}
                    >
                      {ev.amount_milliunits === null
                        ? "—"
                        : formatNok(ev.amount_milliunits)}
                    </td>
                    <td className="max-w-xs truncate px-4 py-3 text-slate-400">
                      {ev.detail ?? <span className="text-slate-600">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {hasMore && (
        <div className="flex justify-center">
          <button
            onClick={() => load({ reset: false })}
            className="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-medium text-slate-200 hover:bg-slate-700/60"
          >
            Load more
          </button>
        </div>
      )}
    </div>
  );
}
