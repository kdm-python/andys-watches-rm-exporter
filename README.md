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
transforms a Medusa order, stores it as `pending` in `data/orders.db`, then
submits it to Royal Mail. Successful submissions are marked `sent`; failures
are marked `failed` with their error message and are retried by the next
request for that order. Orders already marked `sent` are not submitted again.

Run the test suite with:

```sh
uv run pytest
```
