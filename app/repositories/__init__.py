"""Repositories — sole owners of SQL/ORM access per table family.

Pure functions, not classes: leanest form, easy to swap for cached implementations
when we add Redis (just decorate / branch inside the same module). Callers pass
the active SQLAlchemy session in. Repositories never `commit()`; the caller
owns the transaction boundary.
"""
