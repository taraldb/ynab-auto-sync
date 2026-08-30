// Thin fetch wrappers around the FastAPI backend's JSON API.
// All calls are relative paths: in production the built static files are
// served by the same FastAPI app that exposes /api/*.

export interface Account {
  key: string;
  display_name: string;
  ynab_budget: string;
}

export interface RunMetadata {
  last_run_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  auth_required: boolean;
  paused: boolean;
  imported_total: number;
  fetched_last_run: number;
  imported_last_run: number;
  updated_last_run: number;
  duplicates_last_run: number;
  resolved_deleted_last_run: number;
}

export interface StatusResponse {
  run_metadata: RunMetadata;
  accounts: Account[];
  cron_expression: string;
  next_fire_at: string;
  version: string;
  build_timestamp: string;
}

export interface DeletedTransaction {
  sb1_transaction_id: string;
  import_id: string;
  ynab_transaction_id: string;
  ynab_budget_id: string;
  account_key: string;
  booking_status: "DELETED";
  amount_milliunits: number;
  first_seen_at: string;
  last_checked_at: string;
  payee_name: string | null;
  memo: string | null;
  transaction_date: string | null;
  ynab_account_id: string | null;
  cleared: string | null;
  readd_count: number;
}

export interface ReaddResponse {
  new_ynab_transaction_id: string;
}

export type ImportRowStatus = "new" | "duplicate" | "error";

export interface ImportRow {
  row_index: number;
  date: string;
  amount_milliunits: number;
  payee_name: string;
  memo: string | null;
  cleared: string;
  status: ImportRowStatus;
}

export interface ImportSummary {
  new: number;
  duplicate: number;
  errors: number;
}

export interface ImportAccount {
  key: string;
  display_name: string;
  ynab_account_id: string;
  ynab_budget: string;
}

export interface ImportResponse {
  transformer: string;
  account: ImportAccount;
  rows: ImportRow[];
  summary: ImportSummary;
  committed: boolean;
}

export interface TransformerInfo {
  name: string;
  default_ynab_budget_id: string | null;
  default_ynab_budget_alias: string | null;
  default_ynab_account_id: string | null;
}

export class ApiError extends Error {
  status: number;
  detail: string;
  transformer?: string;

  constructor(status: number, detail: string, transformer?: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.transformer = transformer;
  }
}

async function extractDetail(
  res: Response,
): Promise<{ message: string; transformer?: string }> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") {
      return { message: body.detail };
    }
    if (
      body &&
      body.detail &&
      typeof body.detail === "object" &&
      typeof body.detail.message === "string"
    ) {
      return { message: body.detail.message, transformer: body.detail.transformer };
    }
    return { message: JSON.stringify(body) };
  } catch {
    return { message: res.statusText || `Request failed with status ${res.status}` };
  }
}

async function handleJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const { message, transformer } = await extractDetail(res);
    throw new ApiError(res.status, message, transformer);
  }
  return (await res.json()) as T;
}

export async function getStatus(): Promise<StatusResponse> {
  const res = await fetch("/api/status");
  return handleJson<StatusResponse>(res);
}

export async function listDeleted(): Promise<DeletedTransaction[]> {
  const res = await fetch("/api/deleted-transactions");
  return handleJson<DeletedTransaction[]>(res);
}

export async function readd(trackingKey: string): Promise<ReaddResponse> {
  const res = await fetch(
    `/api/deleted-transactions/${encodeURIComponent(trackingKey)}/readd`,
    { method: "POST" },
  );
  return handleJson<ReaddResponse>(res);
}

type ImportTarget =
  | { kind: "mapped"; accountKey: string }
  | { kind: "direct"; ynabBudgetId: string; ynabAccountId: string }
  | undefined;

export async function importFile(
  file: File,
  dryRun: boolean,
  target?: ImportTarget,
): Promise<ImportResponse> {
  const form = new FormData();
  form.append("file", file);
  form.append("dry_run", dryRun ? "true" : "false");
  if (target) {
    if (target.kind === "mapped") {
      form.append("account_key", target.accountKey);
    } else if (target.kind === "direct") {
      form.append("ynab_budget_id", target.ynabBudgetId);
      form.append("ynab_account_id", target.ynabAccountId);
    }
  }
  const res = await fetch("/api/import", {
    method: "POST",
    body: form,
  });
  return handleJson<ImportResponse>(res);
}

export async function listTransformers(): Promise<TransformerInfo[]> {
  const res = await fetch("/api/transformers");
  return handleJson<TransformerInfo[]>(res);
}

export async function updateTransformerDefaults(
  name: string,
  defaults: { ynabBudgetId: string; ynabAccountId: string } | null,
): Promise<TransformerInfo> {
  const res = await fetch(`/api/transformers/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      default_ynab_budget_id: defaults?.ynabBudgetId ?? null,
      default_ynab_account_id: defaults?.ynabAccountId ?? null,
    }),
  });
  return handleJson<TransformerInfo>(res);
}

export interface Mapping {
  id: number;
  provider: string;
  provider_account_id: string;
  ynab_budget_id: string;
  ynab_account_id: string;
  display_name: string;
  import_source_name: string;
  enabled: boolean;
  tracked_count: number;
}

export interface MappingCreate {
  provider: string;
  provider_account_id: string;
  ynab_budget_id: string;
  ynab_account_id: string;
  display_name?: string;
  import_source_name?: string;
  enabled?: boolean;
}

export type MappingUpdate = Partial<MappingCreate>;

export interface ProviderAccount {
  provider_account_id: string;
  display_name: string;
  account_type: string;
  currency: string;
  mapped: boolean;
}

export interface ProviderInfo {
  // The provider's config-key name - this is what account_mappings.provider
  // stores and what must be sent back as `provider` when creating a mapping.
  name: string;
  // Implementation type, for display only. Two connections of the same type
  // are legitimate, so this is NOT a usable identifier.
  type: string;
  auth_required: boolean;
  error: string | null;
  accounts: ProviderAccount[];
}

export interface YnabAccount {
  id: string;
  name: string;
  type: string;
  on_budget: boolean;
}

export interface YnabBudget {
  budget_id: string;
  alias: string;
  accounts: YnabAccount[];
}

export interface SyncNowResponse {
  status: string;
}

// Push messages over /api/ws (see contexts/SyncStatusContext.tsx). Mirrors
// the envelope shape notifications/websocket_sink.py broadcasts:
// {"type": "...", "data": {...}}. Not every type the backend can send is
// modeled here (e.g. "availability"/"state_value" are MQTT/Home Assistant
// concerns the dashboard doesn't consume) - the trailing member keeps
// those, and any future addition, from being a type error.
export interface ProgressState {
  phase: string;
  [key: string]: unknown;
}

export type WsMessage =
  | { type: "status_snapshot"; data: StatusResponse }
  | { type: "status"; data: { run_metadata: RunMetadata } }
  | { type: "sync_state"; data: { value: string } }
  | { type: "cycle_progress"; data: ProgressState }
  | { type: string; data: unknown };

export async function listMappings(): Promise<Mapping[]> {
  const res = await fetch("/api/mappings");
  return handleJson<Mapping[]>(res);
}

export async function createMapping(body: MappingCreate): Promise<Mapping> {
  const res = await fetch("/api/mappings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return handleJson<Mapping>(res);
}

export async function updateMapping(
  id: number,
  body: MappingUpdate,
): Promise<Mapping> {
  const res = await fetch(`/api/mappings/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return handleJson<Mapping>(res);
}

export async function deleteMapping(id: number): Promise<void> {
  const res = await fetch(`/api/mappings/${id}`, { method: "DELETE" });
  if (!res.ok) {
    const { message, transformer } = await extractDetail(res);
    throw new ApiError(res.status, message, transformer);
  }
}

export interface ClearAllMappingsResponse {
  deleted: number;
}

export async function clearAllMappings(): Promise<ClearAllMappingsResponse> {
  const res = await fetch("/api/mappings", { method: "DELETE" });
  return handleJson<ClearAllMappingsResponse>(res);
}

export async function listProviders(
  forceRefresh?: boolean,
): Promise<ProviderInfo[]> {
  const url = forceRefresh ? "/api/providers?force_refresh=true" : "/api/providers";
  const res = await fetch(url);
  return handleJson<ProviderInfo[]>(res);
}

export async function listYnabAccounts(
  forceRefresh?: boolean,
): Promise<YnabBudget[]> {
  const url = forceRefresh
    ? "/api/ynab/accounts?force_refresh=true"
    : "/api/ynab/accounts";
  const res = await fetch(url);
  return handleJson<YnabBudget[]>(res);
}

export async function syncNow(): Promise<SyncNowResponse> {
  const res = await fetch("/api/sync-now", { method: "POST" });
  return handleJson<SyncNowResponse>(res);
}

export interface PauseResponse {
  paused: boolean;
}

export async function setPaused(paused: boolean): Promise<PauseResponse> {
  const res = await fetch("/api/pause", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paused }),
  });
  return handleJson<PauseResponse>(res);
}

export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

export interface Settings {
  log_level: LogLevel;
}

export async function getSettings(): Promise<Settings> {
  const res = await fetch("/api/settings");
  return handleJson<Settings>(res);
}

export async function updateSettings(body: Partial<Settings>): Promise<Settings> {
  const res = await fetch("/api/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return handleJson<Settings>(res);
}

export type AuditEventType = "created" | "updated" | "duplicate" | "skipped";

export type AuditEventSortColumn =
  | "occurred_at"
  | "event_type"
  | "source"
  | "account_key"
  | "payee_name"
  | "memo"
  | "amount_milliunits"
  | "detail";

export type SortDir = "asc" | "desc";

export interface AuditEvent {
  id: number;
  occurred_at: string;
  event_type: AuditEventType;
  source: string;
  account_key: string | null;
  tracking_key: string | null;
  import_id: string | null;
  ynab_transaction_id: string | null;
  ynab_budget_id: string | null;
  ynab_account_id: string | null;
  payee_name: string | null;
  memo: string | null;
  transaction_date: string | null;
  amount_milliunits: number | null;
  detail: string | null;
}

export interface TrackedTransaction {
  sb1_transaction_id: string;
  import_id: string;
  ynab_transaction_id: string;
  ynab_budget_id: string;
  account_key: string;
  booking_status: string;
  amount_milliunits: number;
  first_seen_at: string;
  last_checked_at: string;
  payee_name: string | null;
  memo: string | null;
  transaction_date: string | null;
  ynab_account_id: string | null;
  cleared: string | null;
  readd_count: number;
}

export interface AuditEventDetailResponse {
  event: AuditEvent;
  tracked: TrackedTransaction | null;
}

export interface AuditEventsResponse {
  events: AuditEvent[];
  total: number;
  counts: Record<AuditEventType, number>;
}

export async function listAuditEvents(params: {
  eventType?: AuditEventType | null;
  accountKey?: string | null;
  includeSkipped?: boolean;
  sortBy?: AuditEventSortColumn;
  sortDir?: SortDir;
  limit?: number;
  offset?: number;
}): Promise<AuditEventsResponse> {
  const qs = new URLSearchParams();
  if (params.eventType) qs.set("event_type", params.eventType);
  if (params.accountKey) qs.set("account_key", params.accountKey);
  if (params.includeSkipped) qs.set("include_skipped", "true");
  if (params.sortBy) qs.set("sort_by", params.sortBy);
  if (params.sortDir) qs.set("sort_dir", params.sortDir);
  qs.set("limit", String(params.limit ?? 50));
  qs.set("offset", String(params.offset ?? 0));
  const res = await fetch(`/api/audit-events?${qs.toString()}`);
  return handleJson<AuditEventsResponse>(res);
}

export async function getAuditEvent(id: number): Promise<AuditEventDetailResponse> {
  const res = await fetch(`/api/audit-events/${id}`);
  return handleJson<AuditEventDetailResponse>(res);
}
