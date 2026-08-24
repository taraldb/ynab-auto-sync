import { useEffect, useState } from "react";
import {
  ApiError,
  listDeleted,
  readd,
  type DeletedTransaction,
} from "../api/client";
import { formatDate, formatNok } from "../lib/format";

type RowState = "idle" | "loading" | "success" | "error";

export default function DeletedTransactions() {
  const [rows, setRows] = useState<DeletedTransaction[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [rowState, setRowState] = useState<Record<string, RowState>>({});
  const [rowMessage, setRowMessage] = useState<Record<string, string>>({});

  useEffect(() => {
    load();
  }, []);

  async function load() {
    try {
      const data = await listDeleted();
      setRows(data);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load");
    }
  }

  async function handleReadd(row: DeletedTransaction) {
    const key = row.sb1_transaction_id;
    setRowState((s) => ({ ...s, [key]: "loading" }));
    setRowMessage((s) => ({ ...s, [key]: "" }));
    try {
      const res = await readd(key);
      setRowState((s) => ({ ...s, [key]: "success" }));
      setRowMessage((s) => ({
        ...s,
        [key]: `Re-added as ${res.new_ynab_transaction_id}`,
      }));
    } catch (e) {
      const detail =
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Re-add failed";
      setRowState((s) => ({ ...s, [key]: "error" }));
      setRowMessage((s) => ({ ...s, [key]: detail }));
    }
  }

  if (loadError && !rows) {
    return (
      <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-6 text-rose-200">
        <p className="font-semibold">Couldn't load deleted transactions</p>
        <p className="mt-1 text-sm text-rose-300/80">{loadError}</p>
      </div>
    );
  }

  if (!rows) {
    return (
      <div className="flex items-center gap-3 text-slate-400">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-slate-300" />
        Loading…
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">
            Deleted transactions
          </h1>
          <p className="mt-0.5 text-sm text-slate-400">
            Transactions resolved as permanently deleted in YNAB. Re-adding
            creates a brand new transaction with a fresh import_id.
          </p>
        </div>
        <button
          onClick={load}
          className="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-sm font-medium text-slate-200 hover:bg-slate-700/60"
        >
          Refresh
        </button>
      </div>

      {rows.length === 0 ? (
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-10 text-center text-slate-500">
          Nothing here — no deleted transactions to review.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-900/80 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2.5 font-medium">Date</th>
                <th className="px-4 py-2.5 font-medium">Payee</th>
                <th className="px-4 py-2.5 font-medium">Memo</th>
                <th className="px-4 py-2.5 font-medium">Account</th>
                <th className="px-4 py-2.5 text-right font-medium">Amount</th>
                <th className="px-4 py-2.5 font-medium">Re-adds</th>
                <th className="px-4 py-2.5 font-medium">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800 bg-slate-900/40">
              {rows.map((row) => {
                const key = row.sb1_transaction_id;
                const state = rowState[key] ?? "idle";
                const message = rowMessage[key];
                const isNegative = row.amount_milliunits < 0;

                return (
                  <tr key={key}>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-300">
                      {formatDate(row.transaction_date)}
                    </td>
                    <td className="px-4 py-3 font-medium text-slate-200">
                      {row.payee_name ?? (
                        <span className="text-slate-500">—</span>
                      )}
                    </td>
                    <td className="max-w-xs truncate px-4 py-3 text-slate-400">
                      {row.memo ?? (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-400">
                      {row.account_key}
                    </td>
                    <td
                      className={`whitespace-nowrap px-4 py-3 text-right font-mono tabular-nums ${
                        isNegative ? "text-rose-300" : "text-emerald-300"
                      }`}
                    >
                      {formatNok(row.amount_milliunits)}
                    </td>
                    <td className="px-4 py-3 text-slate-400">
                      {row.readd_count}
                    </td>
                    <td className="px-4 py-3">
                      {state === "success" ? (
                        <div>
                          <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2.5 py-0.5 text-xs font-medium text-emerald-300 ring-1 ring-inset ring-emerald-500/30">
                            Re-added
                          </span>
                          <p className="mt-1 max-w-[16rem] truncate text-xs text-slate-500">
                            {message}
                          </p>
                        </div>
                      ) : (
                        <div>
                          <button
                            onClick={() => handleReadd(row)}
                            disabled={state === "loading"}
                            className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-300 ring-1 ring-inset ring-emerald-500/30 hover:bg-emerald-500/25 disabled:cursor-not-allowed disabled:opacity-60"
                          >
                            {state === "loading" && (
                              <span className="h-3 w-3 animate-spin rounded-full border-2 border-emerald-300/40 border-t-emerald-300" />
                            )}
                            Re-add
                          </button>
                          {state === "error" && (
                            <p className="mt-1 max-w-[16rem] text-xs text-rose-400">
                              {message}
                            </p>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
