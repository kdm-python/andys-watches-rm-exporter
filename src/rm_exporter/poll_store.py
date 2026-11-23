"""Polling observations and a durable inbox; independent of export/dispatch state."""

from contextlib import closing, contextmanager

from psycopg2.extras import Json, RealDictCursor

from rm_exporter.db import get_connection


class QueueConflict(Exception):
    pass


class PollerBusy(Exception):
    pass


def init_polling_db():
    with closing(get_connection()) as conn, conn, conn.cursor() as cursor:
        # API reload and worker startup may initialize these tables together.
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                       ("rm-exporter:poll-schema",))
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS royal_mail_polling (
                medusa_order_id TEXT PRIMARY KEY,
                royal_mail_reference TEXT NOT NULL,
                first_observed_at TIMESTAMPTZ,
                baseline_shipped_on TEXT,
                observation JSONB,
                status TEXT NOT NULL DEFAULT 'watching',
                failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                checked_at TIMESTAMPTZ,
                next_check_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS royal_mail_poll_inbox (
                event_key TEXT PRIMARY KEY,
                medusa_order_id TEXT NOT NULL,
                payload JSONB NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                result JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)


class PollStore:
    def __init__(self, conn):
        self.conn = conn

    def rows(self, statement, args=()):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement, args)
            return [dict(row) for row in cursor.fetchall()]

    def write(self, statement, args=()):
        with self.conn.cursor() as cursor:
            cursor.execute(statement, args)

    def enqueue(self, payload: dict, source: str = "manual") -> dict:
        key = payload["order_id"]
        if payload.get("fulfillment_id"):
            key += ":" + payload["fulfillment_id"]
        self.write("""INSERT INTO royal_mail_poll_inbox
                      (event_key, medusa_order_id, payload, source) VALUES (%s, %s, %s, %s)
                      ON CONFLICT (event_key) DO NOTHING""",
                   (key, payload["order_id"], Json(payload), source))
        row = self.rows("SELECT * FROM royal_mail_poll_inbox WHERE event_key = %s", (key,))[0]
        if row["payload"] != payload:
            raise QueueConflict("An event for this fulfillment already exists with a different payload")
        return row

    def discover(self, order_id=None):
        self.write("""INSERT INTO royal_mail_polling (medusa_order_id, royal_mail_reference)
                      SELECT medusa_order_id, royal_mail_reference FROM royal_mail_exporter
                      WHERE status = 'sent' AND (%s::text IS NULL OR medusa_order_id = %s)
                      ON CONFLICT (medusa_order_id) DO NOTHING""", (order_id, order_id))
        self.write("""UPDATE royal_mail_polling AS p SET status = 'synced'
                      FROM royal_mail_poll_inbox AS i
                      WHERE i.event_key = p.medusa_order_id AND i.status = 'delivered'
                      AND p.status <> 'synced'
                      AND (%s::text IS NULL OR p.medusa_order_id = %s)""", (order_id, order_id))

    def due_orders(self, order_id=None, limit=1000):
        return self.rows("""SELECT * FROM royal_mail_polling WHERE status IN ('watching', 'detected')
                            AND next_check_at <= CURRENT_TIMESTAMP
                            AND (%s::text IS NULL OR medusa_order_id = %s)
                            ORDER BY next_check_at, medusa_order_id LIMIT %s""",
                         (order_id, order_id, limit))

    def observe(self, row, observation, status, interval):
        self.write("""UPDATE royal_mail_polling SET
                      baseline_shipped_on = CASE WHEN first_observed_at IS NULL THEN %s ELSE baseline_shipped_on END,
                      first_observed_at = COALESCE(first_observed_at, CURRENT_TIMESTAMP),
                      observation = %s, status = %s, failures = 0, last_error = NULL,
                      checked_at = CURRENT_TIMESTAMP,
                      next_check_at = CURRENT_TIMESTAMP + %s * INTERVAL '1 second'
                      WHERE medusa_order_id = %s""",
                   (observation["shippedOn"], Json(observation), status, interval, row["medusa_order_id"]))

    def poll_failure(self, row, error, delay):
        self.write("""UPDATE royal_mail_polling SET failures = failures + 1, last_error = %s,
                      checked_at = CURRENT_TIMESTAMP,
                      next_check_at = CURRENT_TIMESTAMP + %s * INTERVAL '1 second'
                      WHERE medusa_order_id = %s""", (error, delay, row["medusa_order_id"]))

    def due_events(self, order_id=None, limit=1000):
        return self.rows("""SELECT * FROM royal_mail_poll_inbox WHERE status IN ('pending', 'retry')
                            AND next_attempt_at <= CURRENT_TIMESTAMP
                            AND (%s::text IS NULL OR medusa_order_id = %s)
                            ORDER BY next_attempt_at, event_key LIMIT %s""",
                         (order_id, order_id, limit))

    def attempted(self, key):
        self.write("""UPDATE royal_mail_poll_inbox SET attempts = attempts + 1,
                      updated_at = CURRENT_TIMESTAMP WHERE event_key = %s""", (key,))

    def event_result(self, key, status, *, error=None, delay=0, result=None):
        self.write("""UPDATE royal_mail_poll_inbox SET status = %s, last_error = %s,
                      result = %s, updated_at = CURRENT_TIMESTAMP,
                      next_attempt_at = CURRENT_TIMESTAMP + %s * INTERVAL '1 second'
                      WHERE event_key = %s""", (status, error, Json(result), delay, key))
        if status == "delivered":
            # Only order-level events settle the one-parcel watcher.
            self.write("UPDATE royal_mail_polling SET status = 'synced' WHERE medusa_order_id = %s", (key,))

    def retry_event(self, key):
        rows = self.rows("SELECT status FROM royal_mail_poll_inbox WHERE event_key = %s", (key,))
        if not rows or rows[0]["status"] == "delivered":
            raise QueueConflict("No unfinished event exists with that key")
        self.event_result(key, "pending")


@contextmanager
def polling_store(*, worker=False):
    with closing(get_connection()) as conn:
        conn.autocommit = True
        if worker:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                               ("rm-exporter:poll-worker",))
                if not cursor.fetchone()[0]:
                    raise PollerBusy("Another polling worker is running")
        yield PollStore(conn)
