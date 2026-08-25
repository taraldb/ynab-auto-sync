---
name: verify-dedup-key-refactor
description: Checklist for verifying a refactor to tracking-key/import_id derivation didn't drift the key. Use whenever key-derivation code is touched in this repo (transform.py, dedup.py, import_ids.py, or similar) — drift here silently re-imports someone's whole financial history as duplicates.
---

Worth reusing whenever key-derivation code is touched, because it's the failure mode that silently re-imports someone's whole financial history:

1. Pin expected `tracking_key`/`import_id` values as **hardcoded literals** in a test — never `assert derive(x) == derive(x)`, which passes happily when both sides drift together.
2. Run identical fixtures through the old and new code paths and diff every field, not just the key.
3. After the change is live, the first real cycle must import **0 new transactions**. A non-zero count means stop immediately and investigate before it writes duplicates.
