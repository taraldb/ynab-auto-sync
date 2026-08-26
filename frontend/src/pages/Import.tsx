import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  getStatus,
  importFile,
  listTransformers,
  listYnabAccounts,
  type Account,
  type ImportResponse,
  type ImportRowStatus,
  type TransformerInfo,
  type YnabBudget,
} from "../api/client";
import { formatNok } from "../lib/format";
import TransformerSettingsModal from "../components/TransformerSettingsModal";

type Phase = "idle" | "previewing" | "previewed" | "confirming" | "committed";

const STATUS_BADGE: Record<ImportRowStatus, string> = {
  new: "bg-emerald-500/15 text-emerald-300 ring-1 ring-inset ring-emerald-500/30",
  duplicate:
    "bg-slate-500/15 text-slate-300 ring-1 ring-inset ring-slate-500/30",
  error: "bg-rose-500/15 text-rose-300 ring-1 ring-inset ring-rose-500/30",
};

function isXlsx(file: File): boolean {
  return (
    file.name.toLowerCase().endsWith(".xlsx") ||
    file.type ===
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
  );
}

export default function Import() {
  const [file, setFile] = useState<File | null>(null);
  const [accountKey, setAccountKey] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [preview, setPreview] = useState<ImportResponse | null>(null);
  const [final, setFinal] = useState<ImportResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountsError, setAccountsError] = useState<string | null>(null);
  const [transformers, setTransformers] = useState<TransformerInfo[]>([]);
  const [budgets, setBudgets] = useState<YnabBudget[]>([]);
  const [showTransformerSettings, setShowTransformerSettings] =
    useState(false);
  const [accountFilterAlias, setAccountFilterAlias] = useState<string | null>(
    null,
  );
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    getStatus()
      .then((res) => {
        if (!cancelled) setAccounts(res.accounts);
      })
      .catch((e) => {
        if (!cancelled) {
          setAccountsError(
            e instanceof Error ? e.message : "Failed to load accounts",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    listTransformers()
      .then((res) => {
        if (!cancelled) setTransformers(res);
      })
      .catch(() => {
        // Non-fatal: the transformer-defaults modal just shows stale/empty
        // data. This is secondary to the core upload flow.
      });
    listYnabAccounts()
      .then((res) => {
        if (!cancelled) setBudgets(res);
      })
      .catch(() => {
        // Non-fatal, same reasoning as above.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function resetForNewFile(f: File | null) {
    setFile(f);
    setPreview(null);
    setFinal(null);
    setErrorDetail(null);
    setPhase("idle");
    setAccountFilterAlias(null);
  }

  function handleFileList(files: FileList | null) {
    if (!files || files.length === 0) return;
    const f = files[0];
    if (!isXlsx(f)) {
      setErrorDetail("That doesn't look like a .xlsx file.");
      return;
    }
    resetForNewFile(f);
  }

  function parseAccountValue(val: string): any {
    if (!val) return undefined;
    if (val.startsWith("mapped:")) {
      return { kind: "mapped", accountKey: val.slice(7) };
    }
    if (val.startsWith("direct:")) {
      const [, budgetId, accountId] = val.split(":");
      return { kind: "direct", ynabBudgetId: budgetId, ynabAccountId: accountId };
    }
    return undefined;
  }

  async function handlePreview() {
    if (!file) return;
    setPhase("previewing");
    setErrorDetail(null);
    try {
      const target = parseAccountValue(accountKey);
      const res = await importFile(file, true, target);
      setPreview(res);
      setPhase("previewed");
    } catch (e) {
      const detail =
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Preview failed";
      setErrorDetail(detail);
      if (e instanceof ApiError && e.transformer) {
        const alias = transformers.find(
          (t) => t.name === e.transformer,
        )?.default_ynab_budget_alias;
        if (alias) setAccountFilterAlias(alias);
      }
      setPhase("idle");
    }
  }

  async function handleConfirm() {
    if (!file) return;
    setPhase("confirming");
    setErrorDetail(null);
    try {
      const target = parseAccountValue(accountKey);
      const res = await importFile(file, false, target);
      setFinal(res);
      setPhase("committed");
    } catch (e) {
      const detail =
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Import failed";
      setErrorDetail(detail);
      if (e instanceof ApiError && e.transformer) {
        const alias = transformers.find(
          (t) => t.name === e.transformer,
        )?.default_ynab_budget_alias;
        if (alias) setAccountFilterAlias(alias);
      }
      setPhase("previewed");
    }
  }

  function startOver() {
    setFile(null);
    setPreview(null);
    setFinal(null);
    setErrorDetail(null);
    setPhase("idle");
    setAccountFilterAlias(null);
    if (inputRef.current) inputRef.current.value = "";
  }

  return (
    <>
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">
            Import from spreadsheet
          </h1>
          <p className="mt-0.5 text-sm text-slate-400">
            Upload a bank export, preview how it will be classified, then
            confirm to actually create transactions in YNAB.
          </p>
        </div>
        <button
          onClick={() => setShowTransformerSettings(true)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-2 text-sm font-medium text-slate-400 hover:bg-slate-700/60 hover:text-slate-200"
          title="Transformer defaults"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-4 w-4"
          >
            <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1.08 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" />
          </svg>
          Transformer defaults
        </button>
      </div>

      {/* Dropzone */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragActive(true);
        }}
        onDragLeave={() => setDragActive(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragActive(false);
          handleFileList(e.dataTransfer.files);
        }}
        onClick={() => inputRef.current?.click()}
        className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed p-10 text-center transition-colors ${
          dragActive
            ? "border-emerald-500 bg-emerald-500/5"
            : "border-slate-700 bg-slate-900/40 hover:border-slate-600"
        }`}
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-8 w-8 text-slate-500"
        >
          <path d="M12 3v12" />
          <path d="m7 8 5-5 5 5" />
          <path d="M5 21h14a2 2 0 0 0 2-2v-5a2 2 0 0 0-2-2h-1" />
          <path d="M5 12H4a2 2 0 0 0-2 2v5a2 2 0 0 0 2 2h1" />
        </svg>
        {file ? (
          <p className="text-sm font-medium text-slate-200">{file.name}</p>
        ) : (
          <>
            <p className="text-sm font-medium text-slate-300">
              Drop a .xlsx file here, or click to browse
            </p>
            <p className="text-xs text-slate-500">
              Only .xlsx spreadsheets are accepted
            </p>
          </>
        )}
        <input
          ref={inputRef}
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          className="hidden"
          onChange={(e) => handleFileList(e.target.files)}
        />
      </div>

      {/* Account override + actions */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="flex flex-col gap-1">
          <label
            htmlFor="account-key"
            className="text-xs font-medium uppercase tracking-wide text-slate-500"
          >
            Account override (optional)
          </label>
          <select
            id="account-key"
            value={accountKey}
            onChange={(e) => setAccountKey(e.target.value)}
            disabled={!!accountsError}
            className="w-64 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-sm text-slate-200 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <option value="">Auto-detect from file format</option>
            <optgroup label="Mapped accounts">
              {(accountFilterAlias
                ? accounts.filter((a) => a.ynab_budget === accountFilterAlias)
                : accounts
              ).map((a) => (
                <option key={a.key} value={`mapped:${a.key}`}>
                  {a.display_name || a.key}
                </option>
              ))}
            </optgroup>
            <optgroup label="All YNAB accounts">
              {(accountFilterAlias
                ? budgets.filter((b) => b.alias === accountFilterAlias)
                : budgets
              ).flatMap((b) =>
                b.accounts.map((a) => (
                  <option key={a.id} value={`direct:${b.budget_id}:${a.id}`}>
                    {a.name} — {b.alias}
                  </option>
                )),
              )}
            </optgroup>
          </select>
          {accountsError && (
            <p className="text-xs text-amber-400">
              Couldn't load accounts: {accountsError}
            </p>
          )}
          {accountFilterAlias && (
            <p className="text-xs text-slate-500">
              Showing accounts in {accountFilterAlias} only (default budget
              for the detected transformer) —{" "}
              <button
                onClick={() => setAccountFilterAlias(null)}
                className="underline hover:text-slate-300"
              >
                Show all
              </button>
            </p>
          )}
        </div>

        <div className="flex gap-2">
          {file && phase !== "committed" && (
            <button
              onClick={startOver}
              className="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-medium text-slate-300 hover:bg-slate-700/60"
            >
              Clear
            </button>
          )}
          <button
            onClick={handlePreview}
            disabled={!file || phase === "previewing" || phase === "confirming"}
            className="inline-flex items-center gap-2 rounded-lg bg-slate-100 px-4 py-2 text-sm font-semibold text-slate-900 hover:bg-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {phase === "previewing" && (
              <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-400/40 border-t-slate-900" />
            )}
            Preview
          </button>
          {phase === "previewed" && preview && (
            <button
              onClick={handleConfirm}
              disabled={phase !== "previewed"}
              className="inline-flex items-center gap-2 rounded-lg bg-emerald-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Confirm Import
            </button>
          )}
        </div>
      </div>

      {phase === "confirming" && (
        <div className="flex items-center gap-2 text-sm text-slate-400">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-slate-300" />
          Creating transactions in YNAB…
        </div>
      )}

      {/* Error callout — backend error details are deliberately witty; show verbatim */}
      {errorDetail && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
          <p className="text-sm font-semibold text-amber-300">
            The backend had something to say
          </p>
          <p className="mt-1 text-sm text-amber-200/90">{errorDetail}</p>
        </div>
      )}

      {/* Final committed state */}
      {phase === "committed" && final && (
        <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-5">
          <p className="text-sm font-semibold text-emerald-300">
            Import complete
          </p>
          <p className="mt-1 text-sm text-emerald-200/90">
            Created {final.summary.new} transaction
            {final.summary.new === 1 ? "" : "s"} in{" "}
            <span className="font-medium">{final.account.ynab_budget}</span>{" "}
            ({final.account.display_name}).
          </p>
        </div>
      )}

      {/* Preview table */}
      {preview && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-medium text-slate-300">
                Preview — {preview.account.display_name} (
                {preview.account.ynab_budget})
              </h2>
              <p className="text-xs text-slate-500">
                Transformer: {preview.transformer}
              </p>
            </div>
            <div className="flex gap-2 text-xs">
              <span className="rounded-full bg-emerald-500/15 px-2.5 py-1 font-medium text-emerald-300 ring-1 ring-inset ring-emerald-500/30">
                {preview.summary.new} new
              </span>
              <span className="rounded-full bg-slate-500/15 px-2.5 py-1 font-medium text-slate-300 ring-1 ring-inset ring-slate-500/30">
                {preview.summary.duplicate} duplicate
              </span>
              <span className="rounded-full bg-rose-500/15 px-2.5 py-1 font-medium text-rose-300 ring-1 ring-inset ring-rose-500/30">
                {preview.summary.errors} errors
              </span>
            </div>
          </div>

          <div className="overflow-x-auto rounded-xl border border-slate-800">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-900/80 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-4 py-2.5 font-medium">Row</th>
                  <th className="px-4 py-2.5 font-medium">Date</th>
                  <th className="px-4 py-2.5 font-medium">Payee</th>
                  <th className="px-4 py-2.5 font-medium">Memo</th>
                  <th className="px-4 py-2.5 text-right font-medium">
                    Amount
                  </th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800 bg-slate-900/40">
                {preview.rows.map((row) => (
                  <tr key={row.row_index}>
                    <td className="px-4 py-2.5 text-slate-500">
                      {row.row_index}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-slate-300">
                      {row.date}
                    </td>
                    <td className="px-4 py-2.5 font-medium text-slate-200">
                      {row.payee_name}
                    </td>
                    <td className="max-w-xs truncate px-4 py-2.5 text-slate-400">
                      {row.memo ?? <span className="text-slate-600">—</span>}
                    </td>
                    <td
                      className={`whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums ${
                        row.amount_milliunits < 0
                          ? "text-rose-300"
                          : "text-emerald-300"
                      }`}
                    >
                      {formatNok(row.amount_milliunits)}
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_BADGE[row.status]}`}
                      >
                        {row.status}
                      </span>
                    </td>
                  </tr>
                ))}
                {preview.rows.length === 0 && (
                  <tr>
                    <td
                      className="px-4 py-6 text-center text-slate-500"
                      colSpan={6}
                    >
                      No rows found in this file.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
    {showTransformerSettings && (
      <TransformerSettingsModal
        transformers={transformers}
        budgets={budgets}
        onUpdate={(updated) =>
          setTransformers((prev) =>
            prev.map((t) => (t.name === updated.name ? updated : t)),
          )
        }
        onClose={() => setShowTransformerSettings(false)}
      />
    )}
    </>
  );
}
