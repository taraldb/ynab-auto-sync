import { useEffect, useState } from "react";
import {
  ApiError,
  clearAllMappings,
  createMapping,
  deleteMapping,
  listMappings,
  listProviders,
  listYnabAccounts,
  updateMapping,
  type Mapping,
  type ProviderAccount,
  type ProviderInfo,
  type YnabAccount,
  type YnabBudget,
} from "../api/client";

function paKey(provider: string, providerAccountId: string): string {
  return `${provider}::${providerAccountId}`;
}

function targetValue(budgetId: string, accountId: string): string {
  return `${budgetId}|${accountId}`;
}

type PendingConfirm =
  | { kind: "remap"; mapping: Mapping; budget: YnabBudget; account: YnabAccount }
  | { kind: "unmap"; mapping: Mapping }
  | { kind: "clear_all" };

export default function Mappings() {
  const [providers, setProviders] = useState<ProviderInfo[] | null>(null);
  const [providersError, setProvidersError] = useState<string | null>(null);
  const [budgets, setBudgets] = useState<YnabBudget[] | null>(null);
  const [budgetsError, setBudgetsError] = useState<string | null>(null);
  const [mappings, setMappings] = useState<Mapping[] | null>(null);
  const [mappingsError, setMappingsError] = useState<string | null>(null);

  const [actionError, setActionError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [dragOverKey, setDragOverKey] = useState<string | null>(null);
  const [draggingKey, setDraggingKey] = useState<string | null>(null);
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(
    null,
  );
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");

  useEffect(() => {
    void loadAll();
  }, []);

  async function loadAll(forceRefresh?: boolean) {
    await Promise.all([
      loadProviders(forceRefresh),
      loadBudgets(forceRefresh),
      loadMappings(),
    ]);
  }

  async function loadProviders(forceRefresh?: boolean) {
    try {
      const data = await listProviders(forceRefresh);
      setProviders(data);
      setProvidersError(null);
    } catch (e) {
      setProvidersError(
        e instanceof Error ? e.message : "Failed to load provider accounts",
      );
    }
  }

  async function loadBudgets(forceRefresh?: boolean) {
    try {
      const data = await listYnabAccounts(forceRefresh);
      setBudgets(data);
      setBudgetsError(null);
    } catch (e) {
      setBudgetsError(
        e instanceof Error ? e.message : "Failed to load YNAB accounts",
      );
    }
  }

  async function loadMappings() {
    try {
      const data = await listMappings();
      setMappings(data);
      setMappingsError(null);
    } catch (e) {
      setMappingsError(
        e instanceof Error ? e.message : "Failed to load mappings",
      );
    }
  }

  function mappingFor(provider: string, providerAccountId: string) {
    return mappings?.find(
      (m) =>
        m.provider === provider && m.provider_account_id === providerAccountId,
    );
  }

  function linkedCountFor(ynabAccountId: string): number {
    // YNAB account ids are globally unique UUIDs, so matching on the id
    // alone (not also budget_id) is safe - no cross-budget collision risk.
    return (
      mappings?.filter((m) => m.ynab_account_id === ynabAccountId).length ?? 0
    );
  }

  async function doCreate(
    provider: string,
    account: ProviderAccount,
    budget: YnabBudget,
    ynabAccount: YnabAccount,
  ) {
    const key = paKey(provider, account.provider_account_id);
    setBusyKey(key);
    setActionError(null);
    try {
      await createMapping({
        provider,
        provider_account_id: account.provider_account_id,
        ynab_budget_id: budget.budget_id,
        ynab_account_id: ynabAccount.id,
        display_name: account.display_name,
      });
      await Promise.all([loadMappings(), loadProviders()]);
    } catch (e) {
      setActionError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Failed to create mapping",
      );
    } finally {
      setBusyKey(null);
    }
  }

  async function doRemap(
    mapping: Mapping,
    budget: YnabBudget,
    ynabAccount: YnabAccount,
  ) {
    const key = paKey(mapping.provider, mapping.provider_account_id);
    setBusyKey(key);
    setActionError(null);
    try {
      await updateMapping(mapping.id, {
        ynab_budget_id: budget.budget_id,
        ynab_account_id: ynabAccount.id,
      });
      await Promise.all([loadMappings(), loadProviders()]);
    } catch (e) {
      setActionError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Failed to update mapping",
      );
    } finally {
      setBusyKey(null);
      setPendingConfirm(null);
    }
  }

  async function doDelete(mapping: Mapping) {
    const key = paKey(mapping.provider, mapping.provider_account_id);
    setBusyKey(key);
    setActionError(null);
    try {
      await deleteMapping(mapping.id);
      await Promise.all([loadMappings(), loadProviders()]);
    } catch (e) {
      setActionError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Failed to remove mapping",
      );
    } finally {
      setBusyKey(null);
      setPendingConfirm(null);
    }
  }

  async function doClearAll() {
    setActionError(null);
    try {
      await clearAllMappings();
      await Promise.all([loadMappings(), loadProviders()]);
    } catch (e) {
      setActionError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Failed to clear all mappings",
      );
    } finally {
      setPendingConfirm(null);
    }
  }

  function startRename(mapping: Mapping) {
    setRenamingId(mapping.id);
    setRenameValue(mapping.display_name);
  }

  function cancelRename() {
    setRenamingId(null);
    setRenameValue("");
  }

  async function doRename(mapping: Mapping) {
    const trimmed = renameValue.trim();
    if (!trimmed || trimmed === mapping.display_name) {
      cancelRename();
      return;
    }
    const key = paKey(mapping.provider, mapping.provider_account_id);
    setBusyKey(key);
    setActionError(null);
    try {
      await updateMapping(mapping.id, { display_name: trimmed });
      await loadMappings();
      cancelRename();
    } catch (e) {
      setActionError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Failed to rename mapping",
      );
    } finally {
      setBusyKey(null);
    }
  }

  async function doToggleEnabled(mapping: Mapping) {
    const key = paKey(mapping.provider, mapping.provider_account_id);
    setBusyKey(key);
    setActionError(null);
    try {
      await updateMapping(mapping.id, { enabled: !mapping.enabled });
      await loadMappings();
    } catch (e) {
      setActionError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Failed to update mapping",
      );
    } finally {
      setBusyKey(null);
    }
  }

  function handleAssign(
    provider: string,
    account: ProviderAccount,
    budget: YnabBudget,
    ynabAccount: YnabAccount,
  ) {
    const existing = mappingFor(provider, account.provider_account_id);
    if (existing) {
      if (
        existing.ynab_budget_id === budget.budget_id &&
        existing.ynab_account_id === ynabAccount.id
      ) {
        return; // already mapped here, nothing to do
      }
      if (existing.tracked_count > 0) {
        setPendingConfirm({ kind: "remap", mapping: existing, budget, account: ynabAccount });
      } else {
        void doRemap(existing, budget, ynabAccount);
      }
      return;
    }
    void doCreate(provider, account, budget, ynabAccount);
  }

  function handleUnmapClick(mapping: Mapping) {
    if (mapping.tracked_count > 0) {
      setPendingConfirm({ kind: "unmap", mapping });
    } else {
      void doDelete(mapping);
    }
  }

  function handleDragStart(
    e: React.DragEvent,
    provider: string,
    account: ProviderAccount,
  ) {
    const key = paKey(provider, account.provider_account_id);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData(
      "text/plain",
      JSON.stringify({ provider, providerAccountId: account.provider_account_id }),
    );
    setDraggingKey(key);
  }

  function handleDrop(
    e: React.DragEvent,
    budget: YnabBudget,
    ynabAccount: YnabAccount,
  ) {
    e.preventDefault();
    setDragOverKey(null);
    setDraggingKey(null);
    let payload: { provider: string; providerAccountId: string };
    try {
      payload = JSON.parse(e.dataTransfer.getData("text/plain"));
    } catch {
      return;
    }
    const providerInfo = providers?.find((p) => p.name === payload.provider);
    const account = providerInfo?.accounts.find(
      (a) => a.provider_account_id === payload.providerAccountId,
    );
    if (!providerInfo || !account) return;
    handleAssign(payload.provider, account, budget, ynabAccount);
  }

  function handleSelectChange(
    provider: string,
    account: ProviderAccount,
    value: string,
  ) {
    if (!value) return;
    const [budgetId, accountId] = value.split("|");
    const budget = budgets?.find((b) => b.budget_id === budgetId);
    const ynabAccount = budget?.accounts.find((a) => a.id === accountId);
    if (!budget || !ynabAccount) return;
    handleAssign(provider, account, budget, ynabAccount);
  }

  const initialLoading =
    providers === null &&
    budgets === null &&
    mappings === null &&
    !providersError &&
    !budgetsError &&
    !mappingsError;

  if (initialLoading) {
    return (
      <div className="flex items-center gap-3 text-slate-400">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-slate-300" />
        Loading…
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">
            Account mappings
          </h1>
          <p className="mt-0.5 text-sm text-slate-400">
            Drag a provider account onto a YNAB account to map it, or use the
            "Map to…" dropdown on a provider account.
          </p>
        </div>
        <button
          onClick={() => void loadAll(true)}
          className="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-sm font-medium text-slate-200 hover:bg-slate-700/60"
        >
          Refresh
        </button>
      </div>

      {actionError && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
          <p className="text-sm font-semibold text-amber-300">
            The backend had something to say
          </p>
          <p className="mt-1 text-sm text-amber-200/90">{actionError}</p>
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Left: provider accounts */}
        <div className="space-y-4">
          <h2 className="text-sm font-medium text-slate-400">
            Provider accounts
          </h2>
          {providersError && (
            <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">
              Couldn't load provider accounts: {providersError}
            </div>
          )}
          {providers?.length === 0 && !providersError && (
            <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-6 text-center text-sm text-slate-500">
              No providers configured.
            </div>
          )}
          {providers?.map((p) => (
            <div
              key={p.name}
              className="rounded-xl border border-slate-800 bg-slate-900/60 p-4"
            >
              <h3 className="text-sm font-semibold text-slate-200">
                {p.name}
              </h3>

              {p.auth_required ? (
                <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
                  Needs re-authentication before its accounts can be listed
                  here.
                </div>
              ) : p.error ? (
                <div className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                  {p.error}
                </div>
              ) : (
                <ul className="mt-3 space-y-2">
                  {p.accounts.map((a) => {
                    const key = paKey(p.name, a.provider_account_id);
                    const existing = mappingFor(
                      p.name,
                      a.provider_account_id,
                    );
                    const isBusy = busyKey === key;
                    const isDragging = draggingKey === key;
                    return (
                      <li
                        key={a.provider_account_id}
                        draggable={!isBusy}
                        onDragStart={(e) =>
                          handleDragStart(e, p.name, a)
                        }
                        onDragEnd={() => setDraggingKey(null)}
                        className={`flex flex-col gap-2 rounded-lg border p-3 transition-colors sm:flex-row sm:items-center sm:justify-between ${
                          isDragging
                            ? "opacity-50"
                            : a.mapped
                              ? "border-emerald-500/30 bg-emerald-500/5"
                              : "border-slate-700 bg-slate-900/40"
                        } ${isBusy ? "cursor-wait opacity-60" : "cursor-grab active:cursor-grabbing"}`}
                      >
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium text-slate-200">
                            {a.display_name}
                          </p>
                          <p className="truncate font-mono text-xs text-slate-500">
                            {a.provider_account_id} · {a.account_type} ·{" "}
                            {a.currency}
                          </p>
                          {existing && (
                            <p className="mt-1 inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-300 ring-1 ring-inset ring-emerald-500/30">
                              Mapped
                              {existing.tracked_count > 0
                                ? ` · ${existing.tracked_count} tracked`
                                : ""}
                            </p>
                          )}
                        </div>
                        <select
                          value={
                            existing
                              ? targetValue(
                                  existing.ynab_budget_id,
                                  existing.ynab_account_id,
                                )
                              : ""
                          }
                          disabled={isBusy || !budgets}
                          onChange={(e) =>
                            handleSelectChange(p.name, a, e.target.value)
                          }
                          className="w-full shrink-0 rounded-lg border border-slate-700 bg-slate-900/60 px-2.5 py-1.5 text-xs text-slate-200 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-50 sm:w-48"
                        >
                          <option value="">
                            {existing ? "Change mapping…" : "Map to…"}
                          </option>
                          {budgets?.map((b) => (
                            <optgroup key={b.budget_id} label={b.alias}>
                              {b.accounts.map((ba) => (
                                <option
                                  key={ba.id}
                                  value={targetValue(b.budget_id, ba.id)}
                                >
                                  {ba.name}
                                  {ba.on_budget ? "" : " (off-budget)"}
                                </option>
                              ))}
                            </optgroup>
                          ))}
                        </select>
                      </li>
                    );
                  })}
                  {p.accounts.length === 0 && (
                    <li className="rounded-lg border border-slate-800 bg-slate-900/30 p-3 text-center text-xs text-slate-500">
                      No accounts found for this provider.
                    </li>
                  )}
                </ul>
              )}
            </div>
          ))}
        </div>

        {/* Right: YNAB budgets/accounts as drop targets */}
        <div className="space-y-4">
          <h2 className="text-sm font-medium text-slate-400">
            YNAB budgets
          </h2>
          {budgetsError && (
            <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">
              Couldn't load YNAB accounts: {budgetsError}
            </div>
          )}
          {budgets?.length === 0 && !budgetsError && (
            <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-6 text-center text-sm text-slate-500">
              No YNAB budgets found.
            </div>
          )}
          {budgets?.map((b) => (
            <div
              key={b.budget_id}
              className="rounded-xl border border-slate-800 bg-slate-900/60 p-4"
            >
              <h3 className="text-sm font-semibold text-slate-200">
                {b.alias}
              </h3>
              <ul className="mt-3 space-y-2">
                {b.accounts.map((a) => {
                  const dropKey = targetValue(b.budget_id, a.id);
                  const isOver = dragOverKey === dropKey;
                  const linkedCount = linkedCountFor(a.id);
                  return (
                    <li
                      key={a.id}
                      onDragOver={(e) => {
                        e.preventDefault();
                        e.dataTransfer.dropEffect = "move";
                        setDragOverKey(dropKey);
                      }}
                      onDragLeave={() =>
                        setDragOverKey((k) => (k === dropKey ? null : k))
                      }
                      onDrop={(e) => handleDrop(e, b, a)}
                      className={`rounded-lg border-2 border-dashed p-3 text-sm transition-colors ${
                        isOver
                          ? "border-emerald-500 bg-emerald-500/10"
                          : "border-slate-700/70 bg-slate-900/40"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <p className="font-medium text-slate-200">{a.name}</p>
                        {linkedCount > 0 && (
                          <span className="shrink-0 rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-300 ring-1 ring-inset ring-emerald-500/30">
                            {linkedCount} linked
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-slate-500">
                        {a.type} · {a.on_budget ? "on budget" : "off budget"}
                      </p>
                    </li>
                  );
                })}
                {b.accounts.length === 0 && (
                  <li className="rounded-lg border border-slate-800 bg-slate-900/30 p-3 text-center text-xs text-slate-500">
                    No accounts in this budget.
                  </li>
                )}
              </ul>
            </div>
          ))}
        </div>
      </div>

      {/* Existing mappings table */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium text-slate-400">
            Existing mappings
          </h2>
          {mappings && mappings.length > 0 && (
            <button
              onClick={() => setPendingConfirm({ kind: "clear_all" })}
              className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-xs font-semibold text-rose-300 hover:bg-rose-500/20"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2.5}
                strokeLinecap="round"
                strokeLinejoin="round"
                className="h-3 w-3"
              >
                <path d="M18 6 6 18" />
                <path d="m6 6 12 12" />
              </svg>
              Clear all
            </button>
          )}
        </div>
        {mappingsError && (
          <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">
            Couldn't load mappings: {mappingsError}
          </div>
        )}
        {mappings?.length === 0 && !mappingsError ? (
          <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-10 text-center text-slate-500">
            No mappings yet — drag a provider account onto a YNAB account to
            create one.
          </div>
        ) : (
          mappings && (
            <div className="overflow-x-auto rounded-xl border border-slate-800">
              <table className="w-full text-left text-sm">
                <thead className="bg-slate-900/80 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-2.5 font-medium">Provider account</th>
                    <th className="px-4 py-2.5 font-medium">YNAB account</th>
                    <th className="px-4 py-2.5 font-medium">Import source</th>
                    <th className="px-4 py-2.5 text-right font-medium">
                      Tracked
                    </th>
                    <th className="px-4 py-2.5 font-medium">Enabled</th>
                    <th className="px-4 py-2.5 font-medium">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800 bg-slate-900/40">
                  {mappings.map((m) => {
                    const key = paKey(m.provider, m.provider_account_id);
                    const isBusy = busyKey === key;
                    const budgetAlias =
                      budgets?.find((b) => b.budget_id === m.ynab_budget_id)
                        ?.alias ?? m.ynab_budget_id;
                    const accountName =
                      budgets
                        ?.find((b) => b.budget_id === m.ynab_budget_id)
                        ?.accounts.find((a) => a.id === m.ynab_account_id)
                        ?.name ?? m.ynab_account_id;
                    const isRenaming = renamingId === m.id;
                    return (
                      <tr key={m.id}>
                        <td className="px-4 py-3">
                          {isRenaming ? (
                            <div className="flex items-center gap-1.5">
                              <input
                                type="text"
                                value={renameValue}
                                autoFocus
                                onChange={(e) => setRenameValue(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") void doRename(m);
                                  if (e.key === "Escape") cancelRename();
                                }}
                                className="w-full rounded-lg border border-slate-700 bg-slate-900/60 px-2 py-1 text-sm text-slate-200 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
                              />
                              <button
                                onClick={() => void doRename(m)}
                                disabled={isBusy}
                                className="shrink-0 rounded-lg bg-emerald-500/15 px-2 py-1 text-xs font-semibold text-emerald-300 ring-1 ring-inset ring-emerald-500/30 hover:bg-emerald-500/25 disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                Save
                              </button>
                              <button
                                onClick={cancelRename}
                                disabled={isBusy}
                                className="shrink-0 rounded-lg border border-slate-700 bg-slate-800/60 px-2 py-1 text-xs font-medium text-slate-300 hover:bg-slate-700/60 disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                Cancel
                              </button>
                            </div>
                          ) : (
                            <button
                              onClick={() => startRename(m)}
                              className="truncate text-left font-medium text-slate-200 hover:underline"
                              title="Click to rename"
                            >
                              {m.display_name}
                            </button>
                          )}
                          <p className="font-mono text-xs text-slate-500">
                            {m.provider} · {m.provider_account_id}
                          </p>
                        </td>
                        <td className="px-4 py-3">
                          <p className="text-slate-200">{accountName}</p>
                          <p className="text-xs text-slate-500">
                            {budgetAlias}
                          </p>
                        </td>
                        <td className="px-4 py-3 text-slate-400">
                          {m.import_source_name || (
                            <span className="text-slate-600">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-right font-mono tabular-nums text-slate-300">
                          {m.tracked_count}
                        </td>
                        <td className="px-4 py-3">
                          <button
                            onClick={() => void doToggleEnabled(m)}
                            disabled={isBusy}
                            className={`rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset disabled:cursor-not-allowed disabled:opacity-50 ${
                              m.enabled
                                ? "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30 hover:bg-emerald-500/25"
                                : "bg-slate-500/15 text-slate-300 ring-slate-500/30 hover:bg-slate-500/25"
                            }`}
                          >
                            {m.enabled ? "Enabled" : "Disabled"}
                          </button>
                        </td>
                        <td className="px-4 py-3">
                          <button
                            onClick={() => handleUnmapClick(m)}
                            disabled={isBusy}
                            className="inline-flex items-center gap-1.5 rounded-lg bg-rose-500/15 px-3 py-1.5 text-xs font-semibold text-rose-300 ring-1 ring-inset ring-rose-500/30 hover:bg-rose-500/25 disabled:cursor-not-allowed disabled:opacity-60"
                          >
                            {isBusy && (
                              <span className="h-3 w-3 animate-spin rounded-full border-2 border-rose-300/40 border-t-rose-300" />
                            )}
                            Unmap
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )
        )}
      </div>

      {/* Remap / unmap / clear-all confirmation */}
      {pendingConfirm && pendingConfirm.kind === "clear_all" && (
        <div className="fixed inset-0 z-20 flex items-center justify-center bg-slate-950/70 p-4">
          <div className="w-full max-w-md rounded-xl border border-rose-500/30 bg-slate-900 p-5 shadow-xl">
            <p className="text-sm font-semibold text-rose-300">
              Remove all {mappings?.length ?? 0} mapping
              {mappings?.length === 1 ? "" : "s"}?
            </p>
            <p className="mt-2 text-sm text-slate-300">
              Every account mapping will be deleted and syncing will stop for
              all of them until you set them up again.{" "}
              {(() => {
                const totalTracked =
                  mappings?.reduce((sum, m) => sum + m.tracked_count, 0) ?? 0;
                return totalTracked > 0 ? (
                  <>
                    Their combined{" "}
                    <span className="font-semibold text-slate-100">
                      {totalTracked} tracked transaction
                      {totalTracked === 1 ? "" : "s"}
                    </span>{" "}
                    will NOT be re-imported when you remap — YNAB permanently
                    reserves the import IDs already used, so that history is
                    never recreated automatically.{" "}
                  </>
                ) : null;
              })()}
              This is irreversible.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setPendingConfirm(null)}
                className="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-medium text-slate-300 hover:bg-slate-700/60"
              >
                Cancel
              </button>
              <button
                onClick={() => void doClearAll()}
                className="rounded-lg bg-rose-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-rose-400"
              >
                Remove all mappings
              </button>
            </div>
          </div>
        </div>
      )}
      {pendingConfirm &&
        (pendingConfirm.kind === "remap" || pendingConfirm.kind === "unmap") && (
          <div className="fixed inset-0 z-20 flex items-center justify-center bg-slate-950/70 p-4">
            <div className="w-full max-w-md rounded-xl border border-amber-500/30 bg-slate-900 p-5 shadow-xl">
              <p className="text-sm font-semibold text-amber-300">
                {pendingConfirm.kind === "remap"
                  ? "Change this mapping?"
                  : "Remove this mapping?"}
              </p>
              <p className="mt-2 text-sm text-slate-300">
                This mapping has{" "}
                <span className="font-semibold text-slate-100">
                  {pendingConfirm.mapping.tracked_count} tracked transaction
                  {pendingConfirm.mapping.tracked_count === 1 ? "" : "s"}
                </span>{" "}
                synced under{" "}
                <span className="font-medium text-slate-100">
                  {pendingConfirm.mapping.display_name}
                </span>
                . {pendingConfirm.kind === "remap" ? "Remapping" : "Unmapping"}{" "}
                will NOT re-import that history into a different account — YNAB
                permanently reserves the import IDs already used for those
                transactions, so they will never be recreated automatically if
                you {pendingConfirm.kind === "remap" ? "change" : "remove"} this
                mapping and set it up again later. This is irreversible.
              </p>
              <div className="mt-4 flex justify-end gap-2">
                <button
                  onClick={() => setPendingConfirm(null)}
                  className="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-medium text-slate-300 hover:bg-slate-700/60"
                >
                  Cancel
                </button>
                <button
                  onClick={() => {
                    if (pendingConfirm.kind === "remap") {
                      void doRemap(
                        pendingConfirm.mapping,
                        pendingConfirm.budget,
                        pendingConfirm.account,
                      );
                    } else {
                      void doDelete(pendingConfirm.mapping);
                    }
                  }}
                  className="rounded-lg bg-rose-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-rose-400"
                >
                  {pendingConfirm.kind === "remap"
                    ? "Change mapping"
                    : "Remove mapping"}
                </button>
              </div>
            </div>
          </div>
        )}
    </div>
  );
}
