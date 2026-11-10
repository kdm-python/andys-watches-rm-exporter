from __future__ import annotations

import os
from contextlib import closing

import psycopg2
from dotenv import load_dotenv
from loguru import logger
from psycopg2.extensions import connection
from psycopg2.extras import RealDictCursor

load_dotenv()


def get_connection() -> connection:
    """Return a connection to the configured PostgreSQL database."""

    url = os.getenv("DB_URL")
    if not url:
        raise RuntimeError("DB_URL must be set to a PostgreSQL connection URL")
    try:
        return psycopg2.connect(url)
    except psycopg2.Error:
        logger.exception("Failed to connect to the database")
        raise


def init_db() -> None:
    """Create the application tables if they do not already exist."""

    with closing(get_connection()) as conn, conn, conn.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS royal_mail_exporter (
                medusa_order_id TEXT PRIMARY KEY,
                royal_mail_reference TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMPTZ,
                error TEXT
            )
        """)
    logger.info("Database initialized successfully.")


def create_order(medusa_order_id: str, royal_mail_reference: str) -> None:
    """Record an order that is awaiting submission to Royal Mail."""

    with closing(get_connection()) as conn, conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO royal_mail_exporter (
                medusa_order_id,
                royal_mail_reference,
                status
            )
            VALUES (%s, %s, 'pending')
            """,
            (medusa_order_id, royal_mail_reference),
        )


def select_order(medusa_order_id: str) -> dict | None:
    """Retrieve a Royal Mail export record by Medusa order ID."""

    with (
        closing(get_connection()) as conn,
        conn,
        conn.cursor(cursor_factory=RealDictCursor) as cursor,
    ):
        cursor.execute(
            """
            SELECT *
            FROM royal_mail_exporter
            WHERE medusa_order_id = %s
            """,
            (medusa_order_id,),
        )
        return cursor.fetchone()


def update_order_status(
    medusa_order_id: str,
    status: str,
    error: str | None = None,
) -> bool:
    """Update the status of a Royal Mail export record."""

    with closing(get_connection()) as conn, conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE royal_mail_exporter
            SET
                status = %s,
                sent_at = CASE WHEN %s = 'sent' THEN CURRENT_TIMESTAMP ELSE sent_at END,
                error = %s
            WHERE medusa_order_id = %s
            """,
            (status, status, error, medusa_order_id),
        )
        return cursor.rowcount == 1


def delete_order(medusa_order_id: str) -> bool:
    with closing(get_connection()) as conn, conn, conn.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM royal_mail_exporter
            WHERE medusa_order_id = %s
            """,
            (medusa_order_id,),
        )
        return cursor.rowcount == 1
