import { useEffect, useState } from "react";
import {
  ApiError,
  updateTransformerDefaultBudget,
  type TransformerInfo,
  type YnabBudget,
} from "../api/client";

interface TransformerSettingsModalProps {
  transformers: TransformerInfo[];
  budgets: YnabBudget[];
  onUpdate: (updated: TransformerInfo) => void;
  onClose: () => void;
}

function TransformerRow({
  transformer,
  budgets,
  onUpdate,
}: {
  transformer: TransformerInfo;
  budgets: YnabBudget[];
  onUpdate: (updated: TransformerInfo) => void;
}) {
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const value = e.target.value || null;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await updateTransformerDefaultBudget(
        transformer.name,
        value,
      );
      onUpdate(updated);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : "Failed to save",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex items-center justify-between gap-3 py-2.5">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-slate-200">
          {transformer.name}
        </p>
        {error && <p className="mt-0.5 text-xs text-rose-400">{error}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {saved && (
          <span className="text-xs font-medium text-emerald-400">Saved</span>
        )}
        <select
          value={transformer.default_ynab_budget_id ?? ""}
          onChange={handleChange}
          disabled={saving}
          className="w-48 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-1.5 text-sm text-slate-200 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <option value="">No default</option>
          {budgets.map((b) => (
            <option key={b.budget_id} value={b.budget_id}>
              {b.alias}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

export default function TransformerSettingsModal({
  transformers,
  budgets,
  onUpdate,
  onClose,
}: TransformerSettingsModalProps) {
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

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
          <p className="text-sm font-semibold text-slate-100">
            Transformer defaults
          </p>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-lg leading-none text-slate-500 hover:text-slate-300"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <div className="max-h-[85vh] overflow-y-auto px-5 py-4">
          <p className="mb-3 text-xs text-slate-500">
            Set a default YNAB budget per transformer to narrow the account
            picker when account resolution fails during import.
          </p>
          {transformers.length === 0 ? (
            <p className="text-sm italic text-slate-500">
              No transformers registered.
            </p>
          ) : (
            <div className="divide-y divide-slate-800">
              {transformers.map((t) => (
                <TransformerRow
                  key={t.name}
                  transformer={t}
                  budgets={budgets}
                  onUpdate={onUpdate}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
