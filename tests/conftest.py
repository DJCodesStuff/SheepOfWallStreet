import os
import uuid

import pytest

from sheep.common.db import SCHEMA, connect


@pytest.fixture
def db():
    """A throwaway schema inside one transaction that is always rolled back."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set (run with `uv run --env-file .env pytest`)")
    with connect() as conn:
        schema = f"test_{uuid.uuid4().hex[:12]}"
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET LOCAL search_path TO {schema}")  # LOCAL: safe with Neon's pooler
        conn.execute(SCHEMA)
        yield conn
        conn.rollback()
