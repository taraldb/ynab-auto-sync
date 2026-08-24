from __future__ import annotations

# Importing the transformers subpackage here guarantees every registered
# transformer is loaded (and its @register decorator fired) simply by
# importing anything from ynab_auto_sync.sync.file_import - callers
# (engine.py, the webapp route) shouldn't need to know this subpackage
# exists just to make detect_transformer() actually find something.
from . import transformers  # noqa: F401
