from __future__ import annotations

import sqlite3
from pathlib import Path

from loguru import logger

DB_PATH = Path("data/orders.db")


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Return a connection to the configured SQLite database."""

    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        logger.exception("Failed to connect to the database")
        raise


def init_db(db_path: Path | None = None) -> None:
    """Create the application tables if they do not already exist."""

    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                medusa_order_id TEXT PRIMARY KEY,
                royal_mail_reference TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT,
                error TEXT
            )
        """)
        logger.info("Database initialized successfully.")


def create_order(
    medusa_order_id: str, royal_mail_reference: str, db_path: Path | None = None
) -> None:
    """Record an order that is awaiting submission to Royal Mail."""

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO orders (
                medusa_order_id,
                royal_mail_reference,
                status
            )
            VALUES (?, ?, 'pending')
            """,
            (medusa_order_id, royal_mail_reference),
        )


def select_order(
    medusa_order_id: str, db_path: Path | None = None
) -> sqlite3.Row | None:
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE medusa_order_id = ?
            """,
            (medusa_order_id,),
        )
        return cursor.fetchone()


def update_order_status(
    medusa_order_id: str, status: str, db_path: Path | None = None
) -> bool:
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE orders
            SET
                status = ?,
                sent_at = CASE WHEN ? = 'sent' THEN CURRENT_TIMESTAMP ELSE sent_at END
            WHERE medusa_order_id = ?
            """,
            (status, status, medusa_order_id),
        )
        return cursor.rowcount == 1


def delete_order(medusa_order_id: str, db_path: Path | None = None) -> bool:
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            DELETE FROM orders
            WHERE medusa_order_id = ?
            """,
            (medusa_order_id,),
        )
        return cursor.rowcount == 1
