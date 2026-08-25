import { type AuditEventType } from "../api/client";

export const TYPE_BADGE: Record<AuditEventType, string> = {
  created: "bg-emerald-500/15 text-emerald-300 ring-1 ring-inset ring-emerald-500/30",
  updated: "bg-sky-500/15 text-sky-300 ring-1 ring-inset ring-sky-500/30",
  duplicate: "bg-amber-500/15 text-amber-300 ring-1 ring-inset ring-amber-500/30",
  skipped: "bg-rose-500/15 text-rose-300 ring-1 ring-inset ring-rose-500/30",
};

export const TILE_LABEL: Record<AuditEventType, string> = {
  created: "Created",
  updated: "Updated",
  duplicate: "Duplicate",
  skipped: "Skipped",
};
