from __future__ import annotations

# Importing each transformer module here triggers its @register decorator,
# populating registry.REGISTRY - matching the ynab-converter precedent
# (transformers/__init__.py's `from . import norwegian_bank` at the bottom
# of that file) exactly. A future transformer module must be imported here
# too, or detect_transformer() will never see it.
from . import norwegian_bank  # noqa: F401
