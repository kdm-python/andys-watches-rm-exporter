import os
from collections.abc import Generator
from contextlib import closing
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import make_dsn


@pytest.fixture
def temporary_database(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")

    schema = f"rm_exporter_test_{uuid4().hex}"
    with closing(psycopg2.connect(url)) as conn:
        with conn, conn.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            monkeypatch.setenv("DB_URL", make_dsn(url, options=f"-c search_path={schema}"))
            yield
        finally:
            with conn, conn.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )
