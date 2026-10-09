# RM Exporter

Install the project dependencies with `uv sync`, then run the CLI with:

```sh
uv run rm-exporter --help
```

For the HTTP API, copy `.env.template` to `.env`, provide the Medusa and
integration credentials, then start Uvicorn:

```sh
uv run uvicorn rm_exporter.api:app --reload
```

`POST /orders` requires an `X-Integration-Key` header. It fetches and
transforms a Medusa order, stores it as `pending` in `data/orders.db`, and
returns the Royal Mail payload without submitting it to Royal Mail.

Run the test suite with:

```sh
uv run pytest
```
