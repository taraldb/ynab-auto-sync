from __future__ import annotations

# This package is the seam for a future multi-provider design: additional
# bank/card aggregators implementing the TransactionProvider contract in
# base.py, registered into registry.REGISTRY the same way
# sync/file_import/transformers are. Purely additive as of its creation -
# nothing in engine.py/scheduler.py consumes it yet, and
# sparebank1/client.py has not been adapted into a provider. Once a real
# provider module exists here, mirror sync/file_import/__init__.py's
# pattern of importing it eagerly so registration happens as a side effect
# of importing this package.
