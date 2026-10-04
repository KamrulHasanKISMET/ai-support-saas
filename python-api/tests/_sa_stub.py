"""Import-time helper: if SQLAlchemy is not installed (build sandbox),
provide MagicMock stand-ins (same approach as conftest_mocks.py) so modules
that `from sqlalchemy import text` / `sqlalchemy.ext.asyncio` import against
fake sessions. With the real library installed (Docker) this does nothing."""
import sys
from unittest.mock import MagicMock

try:  # pragma: no cover
    import sqlalchemy  # noqa: F401
except ImportError:  # pragma: no cover
    m = MagicMock()
    m.text = lambda q: q
    sys.modules["sqlalchemy"] = m
    for sub in ("ext", "ext.asyncio", "orm"):
        sys.modules[f"sqlalchemy.{sub}"] = MagicMock()
