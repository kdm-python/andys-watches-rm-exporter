# RM Exporter

Install the project dependencies with `uv sync`, then run the CLI with:

```sh
uv run rm-exporter --help
```

For the HTTP API, copy `.env.template` to `.env`, provide `DB_URL` (a
PostgreSQL connection URL), and the Medusa and integration credentials, then
start Uvicorn. The database must already exist and the configured user must
be able to create and access tables:

```sh
uv run uvicorn rm_exporter.api:app --reload
```

`POST /orders` requires an `X-Integration-Key` header. It fetches and
transforms a Medusa order, stores it as `pending` in the PostgreSQL
`royal_mail_exporter` table, then
submits it to Royal Mail. Successful submissions are marked `sent`; failures
are marked `failed` with their error message and are retried by the next
request for that order. Orders already marked `sent` are not submitted again.

The table is created on startup if it does not exist. An existing table must
have the same columns and constraints as the table created by the application;
startup does not alter existing tables. Existing SQLite records are not migrated
automatically. Migrate them before switching if their sent statuses are needed
to prevent resubmission.

Run the test suite against a disposable PostgreSQL database with:

```sh
TEST_DATABASE_URL=postgresql://user:password@localhost/test_database uv run pytest
```

The test user must be able to create and drop schemas. Each database test uses
an isolated temporary schema. Without `TEST_DATABASE_URL`, database-dependent
tests are skipped; they never fall back to `DB_URL`.
