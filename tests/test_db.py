import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from rm_exporter import db


@pytest.fixture
def temporary_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path, None, None]:
    """Use a disposable SQLite file, matching the production deployment style."""

    database_path = tmp_path / "orders.db"
    monkeypatch.setattr(db, "DB_PATH", database_path)
    db.init_db()
    yield database_path


def test_create_and_select_order(temporary_database: Path):
    db.create_order("medusa_1", "AW-1001")

    order = db.select_order("medusa_1")

    assert order is not None
    assert order["medusa_order_id"] == "medusa_1"
    assert order["royal_mail_reference"] == "AW-1001"
    assert order["status"] == "pending"
    assert temporary_database.is_file()


def test_create_order_rejects_duplicate_medusa_id(temporary_database: Path):
    db.create_order("medusa_1", "AW-1001")

    with pytest.raises(sqlite3.IntegrityError):
        db.create_order("medusa_1", "AW-1002")


def test_update_and_delete_order(temporary_database: Path):
    db.create_order("medusa_1", "AW-1001")

    assert db.update_order_status("medusa_1", "sent") is True
    order = db.select_order("medusa_1")
    assert order["status"] == "sent"
    assert order["sent_at"] is not None
    assert db.update_order_status("missing", "sent") is False

    assert db.delete_order("medusa_1") is True
    assert db.select_order("medusa_1") is None
    assert db.delete_order("medusa_1") is False
