"""Durable dispatch progress, separate from outbound Royal Mail export records."""

from contextlib import closing, contextmanager

from psycopg2.extras import Json, RealDictCursor

from rm_exporter.db import get_connection


class DispatchBusy(Exception):
    pass


class DispatchStore:
    def __init__(self, conn):
        self.conn = conn

    def get(self, key: str) -> dict | None:
        with self.conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT * FROM royal_mail_dispatches WHERE dispatch_key = %s", (key,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def has_other_unresolved(self, order_id: str, key: str) -> bool:
        with self.conn.cursor() as cursor:
            cursor.execute(
                """SELECT 1 FROM royal_mail_dispatches WHERE medusa_order_id = %s
                   AND dispatch_key <> %s
                   AND status IN ('fulfillment_requested', 'shipment_requested') LIMIT 1""",
                (order_id, key),
            )
            return cursor.fetchone() is not None

    def create(self, key: str, order_id: str, labels: list[dict]) -> dict:
        with self.conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO royal_mail_dispatches
                   (dispatch_key, medusa_order_id, status, labels)
                   VALUES (%s, %s, 'pending', %s)""",
                (key, order_id, Json(labels)),
            )
        return self.get(key)

    def update(self, key: str, status: str, fulfillment_id: str | None = None):
        with self.conn.cursor() as cursor:
            cursor.execute(
                """UPDATE royal_mail_dispatches
                   SET status = %s, fulfillment_id = COALESCE(%s, fulfillment_id),
                       updated_at = CURRENT_TIMESTAMP
                   WHERE dispatch_key = %s""",
                (status, fulfillment_id, key),
            )


@contextmanager
def locked_dispatch(order_id: str):
    # A session advisory lock covers every exporter worker/process and both
    # default-order and explicit-fulfillment requests. Closing releases it even
    # on errors. Autocommit persists each stage BEFORE the external HTTP call;
    # no database transaction is held open across network requests.
    with closing(get_connection()) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                (f"rm-exporter:dispatch:{order_id}",),
            )
            if not cursor.fetchone()[0]:
                raise DispatchBusy(order_id)
        yield DispatchStore(conn)
