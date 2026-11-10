import psycopg2
import pytest
from rm_exporter import db


@pytest.fixture
def initialized_database(temporary_database: None) -> None:
    db.init_db()
    db.init_db()


def test_missing_database_url_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DB_URL", raising=False)
    with pytest.raises(RuntimeError, match="DB_URL must be set"):
        db.get_connection()


def test_create_and_select_order(initialized_database: None):
    db.create_order("medusa_1", "AW-1001")

    order = db.select_order("medusa_1")

    assert order is not None
    assert order["medusa_order_id"] == "medusa_1"
    assert order["royal_mail_reference"] == "AW-1001"
    assert order["status"] == "pending"
    assert order["created_at"] is not None
    assert order["sent_at"] is None


def test_create_order_rejects_duplicate_medusa_id(initialized_database: None):
    db.create_order("medusa_1", "AW-1001")

    with pytest.raises(psycopg2.IntegrityError):
        db.create_order("medusa_1", "AW-1002")

    assert db.select_order("medusa_1")["royal_mail_reference"] == "AW-1001"


def test_update_order_records_and_clears_an_error(initialized_database: None):
    db.create_order("medusa_1", "AW-1001")

    assert db.update_order_status("medusa_1", "failed", error="Connection timed out")
    assert db.select_order("medusa_1")["error"] == "Connection timed out"
    assert db.select_order("medusa_1")["sent_at"] is None

    assert db.update_order_status("medusa_1", "sent")
    assert db.select_order("medusa_1")["error"] is None
    sent_at = db.select_order("medusa_1")["sent_at"]
    assert sent_at is not None
    assert db.update_order_status("medusa_1", "failed")
    assert db.select_order("medusa_1")["sent_at"] == sent_at


def test_update_and_delete_order(initialized_database: None):
    db.create_order("medusa_1", "AW-1001")

    assert db.update_order_status("medusa_1", "sent") is True
    order = db.select_order("medusa_1")
    assert order["status"] == "sent"
    assert order["sent_at"] is not None
    assert db.update_order_status("missing", "sent") is False

    assert db.delete_order("medusa_1") is True
    assert db.select_order("medusa_1") is None
    assert db.delete_order("medusa_1") is False


def test_sql_values_are_parameterized(initialized_database: None):
    order_id = "order' OR '1'='1"
    db.create_order(order_id, "AW-'1001")
    assert db.select_order(order_id)["royal_mail_reference"] == "AW-'1001"
    assert db.select_order("missing' OR '1'='1") is None
    assert not db.delete_order("missing' OR '1'='1")
    assert db.select_order(order_id) is not None
