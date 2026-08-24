import { useState } from "react";
import { SyncStatusProvider } from "./contexts/SyncStatusContext";
import AuditLog from "./pages/AuditLog";
import Dashboard from "./pages/Dashboard";
import DeletedTransactions from "./pages/DeletedTransactions";
import Import from "./pages/Import";
import Mappings from "./pages/Mappings";

type Tab = "dashboard" | "mappings" | "deleted" | "import" | "audit";

const TABS: { id: Tab; label: string }[] = [
  { id: "dashboard", label: "Dashboard" },
  { id: "mappings", label: "Mappings" },
  { id: "deleted", label: "Deleted Transactions" },
  { id: "import", label: "Import" },
  { id: "audit", label: "Audit Log" },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("dashboard");

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur sticky top-0 z-10">
        <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
          <div className="flex h-16 items-center justify-between">
            <div className="flex items-center gap-2.5">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-500/15 text-emerald-400">
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-4.5 w-4.5"
                >
                  <path d="M17 3v10.55a4 4 0 1 1-2-3.46V3z" />
                  <path d="M7 21v-8" />
                  <path d="M3 17h8" />
                </svg>
              </div>
              <span className="text-sm font-semibold tracking-tight text-slate-100">
                ynab-auto-sync
              </span>
            </div>
            <nav className="flex gap-1 rounded-lg bg-slate-800/60 p-1">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setTab(t.id)}
                  className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                    tab === t.id
                      ? "bg-slate-100 text-slate-900 shadow-sm"
                      : "text-slate-300 hover:bg-slate-700/60 hover:text-slate-100"
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </nav>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
        <SyncStatusProvider>
          {tab === "dashboard" && <Dashboard />}
          {tab === "mappings" && <Mappings />}
          {tab === "deleted" && <DeletedTransactions />}
          {tab === "import" && <Import />}
          {tab === "audit" && <AuditLog />}
        </SyncStatusProvider>
      </main>
    </div>
  );
}
