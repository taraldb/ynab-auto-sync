# ynab-auto-sync

Polls SpareBank1 for bank transactions and imports them into YNAB, with zero
duplicates, full status/control exposed over MQTT for Home Assistant, a
bundled admin GUI, and a spreadsheet-import path for historical bank exports.

## How it works

- A long-running Python service polls SpareBank1's personal-banking API on a
  cron schedule (default: six times a day, nothing overnight — configurable
  via `sync.cron_expression`), transforms new transactions into YNAB's
  format, and bulk-creates them in your YNAB budget.
- **No duplicates**: every transaction gets a deterministic `import_id`
  derived from its SpareBank1 id. YNAB itself rejects re-imports of an
  existing `import_id` on the same account, so duplicates can't happen even
  if local state is lost and the service re-fetches weeks of history.
- **Pending transactions** (credit card authorizations, etc.) import
  immediately as `uncleared`, tracked locally in SQLite. Once SpareBank1
  reports them as booked, the *same* YNAB transaction is updated in place
  (cleared status + final amount) instead of creating a second entry.
- **Multiple YNAB budgets**: each SpareBank1 account maps to a YNAB budget
  by a human-readable alias you define in config, not a raw UUID - so e.g.
  a shared credit card can route into a different budget than your personal
  checking account.
- Status (last run, last success, counts, errors) and commands (sync now,
  pause/resume) are exposed over MQTT using Home Assistant's MQTT Discovery
  format, so a "YNAB Auto Sync" device with sensors/switch/button just shows
  up in Home Assistant automatically.
- Read-only API calls (fetching accounts/transactions/budgets) retry up to 3
  times with backoff on transient network failures or a 429/5xx response —
  a single flaky request no longer fails an entire sync cycle. Writes
  (creating/updating YNAB transactions) are deliberately not retried at this
  layer, since they already get a safe, idempotent retry on the *next* sync
  cycle via the overlap window and YNAB's own `import_id` dedup.
- A bundled admin GUI (enabled by default, port 8080) covers the handful of
  tasks MQTT/Home Assistant can't: a live status dashboard, re-adding a
  transaction that was resolved as deleted-in-YNAB, and importing a bank
  export spreadsheet. See "Admin GUI" below.
- Transactions can also be imported from a spreadsheet export (currently:
  Norwegian Bank's `.xlsx` format) via the GUI's Import tab — useful for
  backfilling history from an account not wired into the live SpareBank1
  poll. Uses the exact same dedup guarantee as the live path (a distinct,
  collision-safe `import_id` domain), so re-uploading the same file is a
  safe no-op. See "Importing a bank export file" below.

See `/Users/tarald/.claude/plans/you-re-the-orchestrator-and-wobbly-turtle.md`
for the full design rationale.

## One-time setup

### 1. Configure

```sh
cp config/config.example.yaml config/config.yaml
```

Fill in `config/config.yaml` (gitignored, never commit it):
- `providers.<name>.client_id` / `client_secret` from your registered client
  at https://developer.sparebank1.no. `<name>` is a key you choose (e.g.
  `sparebank1`); it identifies this connection in account mappings and names
  its token file, so pick one you're happy to keep — renaming it later
  orphans that provider's mappings until you re-point them. You can add
  several entries, each with its own credentials.
- `providers.<name>.redirect_uri` must match exactly what you registered there
  (default `http://localhost:8765/callback`)
- `ynab.personal_access_token` from YNAB → Account Settings → Developer Settings
- `ynab.budgets`: one or more `alias: budget-uuid` entries (aliases are your
  own names, e.g. `personal`, `shared` - find the real UUIDs by running
  `scripts/verify_ynab.py` with any `--budget-alias`, which lists them)
- **account mappings are not configured in this file.** Map bank accounts to
  YNAB accounts in the GUI's **Mappings** tab (drag a provider account onto a
  YNAB account); they're stored in the database so they can be changed
  without a restart. The commented-out `accounts:` block in the example file
  exists only to migrate a pre-existing install: if present on first startup
  it seeds the database once and is ignored from then on.
- `mqtt.*` for your broker (the one your Home Assistant instance already uses)
- `sync.cron_expression` (default `"0 6,8,10,12,16,20 * * *"`) and
  `sync.retention_days` (default `270` — how long a booked, tracked
  transaction is kept locally before it's eligible for pruning)
- `gui.*` for the bundled admin GUI — `enabled` (default `true`), `host`,
  `port` (default `8080`)

### 2. Install dependencies

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Interactive BankID login (must be run on a machine with a browser)

```sh
python scripts/auth_setup.py
```

Opens a browser for BankID login, captures the OAuth code on a temporary
local server, exchanges it for tokens, and smoke-tests them. Tokens land in
`state/tokens/<provider-name>.json`. If you run this somewhere other than your docker host,
copy that file onto the host's `state/` volume before starting the service.

The refresh token is valid **365 days** and rotates every time the service
refreshes it — as long as the service runs at least once within any 365-day
window, it keeps itself authenticated indefinitely. If it doesn't (e.g. the
host was off for over a year), just re-run this script.

### 4. Verify the two things SpareBank1/YNAB's docs didn't fully specify

These are real unknowns flagged in the design — resolve them once, against
your own live account, before trusting the service with real data:

```sh
python scripts/probe_transactions.py --account-key <a-sparebank1-account-key>
```
Confirms whether `fromDate`/`toDate` actually filter server-side and what
the real transaction id/date/amount field names are. If they differ from
the guesses in `src/ynab_auto_sync/sparebank1/client.py` (`TX_PARAM_*`) or
`src/ynab_auto_sync/sync/transform.py` (`*_FIELD_CANDIDATES`), update those
constants — they're centralized specifically so this is a small, localized
fix.

```sh
python scripts/verify_ynab.py --account-id <a-ynab-account-id>
```
Confirms the `/budgets` vs `/plans` resource path (update
`RESOURCE_PATH` in `src/ynab_auto_sync/ynab/client.py` if it turns out to be
`plans`) and directly proves the dedup guarantee: it creates one test
transaction, submits it a second time, and confirms YNAB reports the second
attempt as a duplicate rather than creating it again. Pass `--budget-alias`
if you've configured more than one budget.

```sh
python scripts/verify_ynab_update.py --account-id <a-ynab-account-id>
```
Confirms the bulk transaction *update* call (used to flip a pending
transaction to cleared once it books) actually changes an existing
transaction in place rather than erroring or creating a duplicate - creates
one uncleared test transaction, updates its cleared status and amount, and
reads it back to confirm both changed.

**Known open assumption to watch after deploying**: the pending→booked
reconciliation assumes SpareBank1 keeps a transaction's `id` stable from
when it's first seen as `PENDING` through to `BOOKED`. This can't be
verified synchronously - watch a real pending transaction (e.g. a card
purchase) settle over the following day or two, and confirm in YNAB that it
flips to cleared in place rather than appearing as a second transaction.

**Contingency if that assumption turns out false**: a comparison against a
similar sibling project confirmed there's no way to verify this ahead of
time, and that project's own bank integration sidesteps the question
entirely - it never syncs pending transactions at all, only ones already
booked. If SpareBank1 turns out to issue a new id once a transaction
settles (so the update-in-place never fires and a duplicate cleared entry
appears instead), the fallback is the same: revert `sync/transform.py`'s
`cleared` derivation and `sync/engine.py`'s classification to skip
`PENDING` transactions until they're `BOOKED`, rather than attempting
fuzzy date/amount matching to reconcile them after the fact.

### 5. Run the tests

```sh
pytest -q
ruff check src scripts tests
```

### 6. Try it locally

```sh
docker compose up --build
```

Runs the service against a local mosquitto broker (see `dev/mosquitto.conf`).
Watch discovery and state topics with:

```sh
mosquitto_sub -h localhost -t 'homeassistant/#' -v
mosquitto_sub -h localhost -t 'ynab_auto_sync/#' -v
```

Trigger a manual sync or pause it:

```sh
mosquitto_pub -h localhost -t ynab_auto_sync/command/sync_now -m PRESS
mosquitto_pub -h localhost -t ynab_auto_sync/command/pause -m ON
```

## Deploying to your docker host

Images are built by GitHub Actions and published to
`ghcr.io/<owner>/<repo>` on every push to `main` (tag `latest`) and on `v*`
tags (semver tags). `linux/amd64` only.

```sh
docker run -d \
  --name ynab-auto-sync \
  --restart unless-stopped \
  -p 8080:8080 \
  -v /path/to/config:/app/config \
  -v /path/to/state:/app/state \
  ghcr.io/<owner>/<repo>:latest
```

Drop `-p 8080:8080` if you're reverse-proxying to the container over a
Docker network instead of a published port (see "Admin GUI" below), or set
`gui.enabled: false` in config to disable the GUI entirely and stay fully
headless.

Both `config/` (your secrets, mostly static) and `state/` (rotating OAuth
tokens in `tokens/<provider-name>.json`, plus account mappings, sync cursors and transaction tracking in the
SQLite database `sync_state.db`, written every cycle) must be persistent
volumes — losing `state/tokens/` means re-running `scripts/auth_setup.py`, and losing `sync_state.db` also loses your account mappings (transactions themselves stay safe: YNAB's `import_id` is the real dedup guarantee).
Losing `sync_state.db` is safe by design (see the no-duplicates guarantee
above) but does reset the pending→booked tracking for any transaction still
mid-flight at the time.

To re-authenticate on the host itself (rather than copying a file over):

```sh
docker run --rm -it \
  -p 8765:8765 \
  -v /path/to/config:/app/config \
  -v /path/to/state:/app/state \
  --entrypoint python \
  ghcr.io/<owner>/<repo>:latest scripts/auth_setup.py
```

(needs a browser reachable at that host/port — usually easier to run
`scripts/auth_setup.py` locally and copy `state/tokens/` over instead).

## Home Assistant

Nothing to configure manually if MQTT discovery is enabled in Home
Assistant — a "YNAB Auto Sync" device appears with:

- **Sensors**: Sync State, Last Run, Last Successful Sync, Imported (Last
  Run), Updated to Cleared (Last Run), Duplicates Skipped (Last Run),
  Imported (Total), Last Error
- **Binary sensors**: Problem, Auth Required
- **Switch**: Sync Paused
- **Button**: Sync Now

`Auth Required` turning on means the refresh token was rejected — re-run
`scripts/auth_setup.py`. A generic `Problem` means the last cycle failed for
some other reason — check `Last Error` and the container logs (JSON
formatted, one line per event).

## Admin GUI

A bundled React SPA at `http://<host>:8080/` (or wherever you proxy it),
covering four things MQTT/Home Assistant can't:

- **Dashboard** — the same status MQTT publishes (last run/success, error,
  paused state, per-run and all-time counts), polled live.
- **Deleted transactions** — lists any transaction YNAB reports as
  permanently deleted (see the no-duplicates guarantee above), with a
  one-click re-add that recreates it from the locally persisted
  payee/memo/date/amount using a fresh `import_id`.
- **Mappings** — drag a bank account onto a YNAB account to map it (with a
  dropdown as a non-drag alternative), toggle a mapping on/off, or unmap it.
  Changing or removing a mapping that already has synced transactions asks
  for confirmation first: YNAB permanently reserves the import ids already
  used, so **that history will not be re-imported into a different account**.
- **Import** — upload a bank export spreadsheet; see "Importing a bank
  export file" below.

**The GUI has no application-level authentication of its own, by design** —
it can trigger real financial actions (creating YNAB transactions), so
don't expose it directly to the internet. Put it behind something that
authenticates first: a VPN, a reverse proxy with basic auth, or
Cloudflare Access. `deploy/swag/ynab.subdomain.conf` is a sample
[SWAG](https://github.com/linuxserver/docker-swag) reverse-proxy config
(copy it into your SWAG container's `nginx/proxy-confs/`) to expose it at
e.g. `ynab.yourdomain.com`, with Cloudflare Access (or similar) doing the
actual authentication in front of it.

Set `gui.enabled: false` in config to disable it entirely and run fully
headless, as before.

## Importing a bank export file

For accounts not wired into the live SpareBank1 poll (or to backfill
history), upload a spreadsheet export via the GUI's Import tab:

1. Upload the file — it's parsed and matched against a registered format
   (currently: Norwegian Bank's `.xlsx` export) and previewed with each row
   classified as new / already-imported (duplicate) / unparseable (error),
   with no transactions created yet.
2. Confirm — resubmits the same file to actually create the "new" rows in
   YNAB.

The target account is either the one you pick in the GUI, or — if you leave
it unset — whichever configured account has a matching `import_source_name`.
If neither resolves, or the file's format isn't recognized, the import is
rejected with an explanation of what's missing (config-side, not a bug).

Re-uploading the same file (or one with overlapping transactions) is always
safe: each row's `import_id` is a deterministic hash of its date, amount,
payee, and memo, so already-imported rows are recognized as duplicates and
skipped, never re-created.

New bank formats are added as a new transformer under
`src/ynab_auto_sync/sync/file_import/transformers/` (see that module's
`TransformerBase` for the plugin contract) — no changes needed to the sync
engine, dedup logic, or the GUI itself.
