"""Retired legacy tooling that is not part of the active runtime.

Modules here implement the wire protocol of an **external index bridge that has
been retired**. They are kept only for operator-side archive/GC tooling and are
not reachable from any active runtime path: the ingress delivery worker, the
recall surfaces and the deploy configuration never construct them.

See ``docs/retired/ragflow-retirement.md`` for the retirement record.
"""
