import { useEffect, useState } from "react";
import {
  getAuditEvent,
  type Account,
  type AuditEvent,
  type TrackedTransaction,
} from "../api/client";
import { formatDate, formatDateTime, formatNok } from "../lib/format";
import { TYPE_BADGE, TILE_LABEL } from "../lib/auditEventStyles";

interface AuditEventModalProps {
  eventId: number;
  initialEvent: AuditEvent;
  accounts: Account[];
  onClose: () => void;
}

function accountLabel(accounts: Account[], key: string): string {
  return accounts.find((a) => a.key === key)?.display_name || key;
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
        {label}
      </p>
      <p className="mt-0.5 text-sm text-slate-200">{value}</p>
    </div>
  );
}

function CopyableIdentifier({
  label,
  value,
}: {
  label: string;
  value: string | null;
}) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be denied by the browser - not worth
      // surfacing an error for a convenience action like this.
    }
  }

  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="shrink-0 text-xs font-medium uppercase tracking-wide text-slate-500">
        {label}
      </span>
      <div className="flex min-w-0 items-center gap-2">
        {value ? (
          <>
            <span className="truncate font-mono text-xs text-slate-300">
              {value}
            </span>
            <button
              onClick={handleCopy}
              className={`shrink-0 rounded-md border px-1.5 py-0.5 text-[11px] font-medium transition-colors ${
                copied
                  ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                  : "border-slate-700 bg-slate-800/60 text-slate-400 hover:bg-slate-700/60 hover:text-slate-200"
              }`}
              title="Copy to clipboard"
            >
              {copied ? "Copied" : "⧉"}
            </button>
          </>
        ) : (
          <span className="text-xs text-slate-600">—</span>
        )}
      </div>
    </div>
  );
}

export default function AuditEventModal({
  eventId,
  initialEvent,
  accounts,
  onClose,
}: AuditEventModalProps) {
  const [event] = useState<AuditEvent>(initialEvent);
  const [tracked, setTracked] = useState<TrackedTransaction | null>(null);
  const [trackedLoading, setTrackedLoading] = useState(true);
  const [trackedError, setTrackedError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setTrackedLoading(true);
    setTrackedError(false);
    getAuditEvent(eventId)
      .then((res) => {
        if (cancelled) return;
        setTracked(res.tracked);
      })
      .catch(() => {
        if (cancelled) return;
        setTrackedError(true);
      })
      .finally(() => {
        if (cancelled) return;
        setTrackedLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [eventId]);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const isNegative =
    event.amount_milliunits !== null && event.amount_milliunits < 0;

  return (
    <div
      className="fixed inset-0 z-20 flex items-center justify-center bg-slate-950/70 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="w-full max-w-lg rounded-xl border border-slate-800 bg-slate-900 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 border-b border-slate-800 px-5 py-4">
          <div className="flex items-center gap-3">
            <span
              className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${TYPE_BADGE[event.event_type]}`}
            >
              {TILE_LABEL[event.event_type]}
            </span>
            <span className="text-sm text-slate-400">
              {formatDateTime(event.occurred_at)}
            </span>
          </div>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-lg leading-none text-slate-500 hover:text-slate-300"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <div className="max-h-[85vh] space-y-5 overflow-y-auto px-5 py-4">
          {event.detail && (
            <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-3">
              <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
                Detail
              </p>
              <p className="mt-1 text-sm leading-relaxed text-slate-200">
                {event.detail}
              </p>
            </div>
          )}

          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Transaction summary
            </h3>
            <div className="grid grid-cols-2 gap-x-4 gap-y-3 rounded-lg border border-slate-800 bg-slate-900/60 p-3">
              <Field
                label="Payee"
                value={
                  event.payee_name ?? (
                    <span className="text-slate-600">—</span>
                  )
                }
              />
              <Field
                label="Amount"
                value={
                  event.amount_milliunits === null ? (
                    <span className="text-slate-600">—</span>
                  ) : (
                    <span
                      className={`font-mono tabular-nums ${
                        isNegative ? "text-rose-300" : "text-emerald-300"
                      }`}
                    >
                      {formatNok(event.amount_milliunits)}
                    </span>
                  )
                }
              />
              <Field
                label="Memo"
                value={
                  event.memo ?? <span className="text-slate-600">—</span>
                }
              />
              <Field
                label="Transaction date"
                value={formatDate(event.transaction_date)}
              />
              <Field label="Source" value={event.source} />
              <Field
                label="Account"
                value={
                  event.account_key ? (
                    accountLabel(accounts, event.account_key)
                  ) : (
                    <span className="text-slate-600">—</span>
                  )
                }
              />
            </div>
          </div>

          <div>

            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Local tracking state
            </h3>
            <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-3">
              {trackedLoading ? (
                  <div className="flex items-center gap-2 text-sm text-slate-400">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-slate-300" />
                    Loading…
                  </div>
              ) : trackedError ? (
                  <p className="text-sm text-rose-400">
                    Couldn't load tracking state.
                  </p>
              ) : tracked ? (
                  <div className="grid grid-cols-2 gap-x-4 gap-y-3">
                    <Field label="Booking status" value={tracked.booking_status} />
                    <Field
                        label="Cleared"
                        value={
                            tracked.cleared ?? (
                                <span className="text-slate-600">—</span>
                            )
                        }
                    />
                    <Field
                        label="First seen"
                        value={formatDateTime(tracked.first_seen_at)}
                    />
                    <Field
                        label="Last checked"
                        value={formatDateTime(tracked.last_checked_at)}
                    />
                    <Field label="Re-add count" value={tracked.readd_count} />
                  </div>
              ) : (
                  <p className="text-sm italic text-slate-500">
                    No longer tracked locally — this row may have been pruned
                    by the retention policy, or this event type never had a
                    tracked record.
                  </p>
              )}
            </div>
          </div>

          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Identifiers
            </h3>
            <div className="divide-y divide-slate-800 rounded-lg border border-slate-800 bg-slate-900/60 px-3">
              <CopyableIdentifier
                label="Tracking key"
                value={event.tracking_key}
              />
              <CopyableIdentifier label="Import ID" value={event.import_id} />
              <CopyableIdentifier
                label="YNAB transaction"
                value={event.ynab_transaction_id}
              />
              <CopyableIdentifier
                label="YNAB budget"
                value={event.ynab_budget_id}
              />
              <CopyableIdentifier
                label="YNAB account"
                value={event.ynab_account_id}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
