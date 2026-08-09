# Data files

`executive_trades.csv` and `executive_trades.json` are written here by
`scripts/export.py` (see the repository root README for schema and
provenance).

The first export has not run yet — it requires the `DC_API_KEY` repository
secret (a free DisclosedCapitol API key from
https://www.disclosedcapitol.com/data-files/api). Once the secret is set,
the nightly workflow (or a manual `workflow_dispatch` run of
`.github/workflows/refresh.yml`) will populate this directory and keep it
current.
