#!/usr/bin/env python3
"""Export the executive-branch trades dataset from the DisclosedCapitol API.

Pulls every executive-branch official (President, VP, Cabinet, senior
appointees) and their full disclosed transaction history, then writes
deterministic, diff-friendly CSV and JSON files plus a coverage-stats
block in README.md.

Endpoints used (all public REST, https://api.disclosedcapitol.com):
  GET /executive/officials                     enumerate officials + tenures
  GET /executive/officials/{id}/profile        filings manifest (278/278-T)
                                               + recent trades w/ source doc
  GET /politicians/{id}/trades                 full trade history (paginated)

Auth: free API key in the DC-API-Key header, read from env DC_API_KEY.
Get one at https://www.disclosedcapitol.com/data-files/api

Stdlib only — no dependencies to install.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = os.environ.get("DC_API_BASE", "https://api.disclosedcapitol.com")
SITE_BASE = "https://www.disclosedcapitol.com"
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
README = REPO_ROOT / "README.md"

COLUMNS = [
    "filer_name",
    "role_title",
    "agency",
    "ticker",
    "asset_description",
    "transaction_type",
    "transaction_date",
    "amount_range",
    "filing_date",
    "filing_type",
    "source_filing_id",
    "disclosedcapitol_url",
]

MAX_RETRIES = 5
PAGE_LIMIT_OFFICIALS = 500
PAGE_LIMIT_TRADES = 1000


def api_get(path: str, params: dict | None = None) -> dict | list:
    """GET a DisclosedCapitol API path with retries and backoff."""
    key = os.environ.get("DC_API_KEY", "").strip()
    if not key:
        sys.exit(
            "ERROR: DC_API_KEY is not set.\n"
            "Create a free API key at https://www.disclosedcapitol.com/data-files/api\n"
            "then run:  DC_API_KEY=dc_... python3 scripts/export.py"
        )
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(
            url,
            headers={
                "DC-API-Key": key,
                "Accept": "application/json",
                "User-Agent": "executive-branch-trades-export/1.0 "
                "(https://github.com/disclosedcapitol/executive-branch-trades)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                sleep_s = min(2**attempt, 30)
                print(f"  HTTP {e.code} on {path} — retrying in {sleep_s}s", flush=True)
                time.sleep(sleep_s)
                last_err = e
                continue
            body = e.read().decode("utf-8", "replace")[:500]
            sys.exit(f"ERROR: HTTP {e.code} on {url}\n{body}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < MAX_RETRIES:
                time.sleep(min(2**attempt, 30))
                last_err = e
                continue
            sys.exit(f"ERROR: request failed for {url}: {e}")
    sys.exit(f"ERROR: exhausted retries for {url}: {last_err}")


def fetch_officials() -> list[dict]:
    """All executive officials with their tenure history."""
    officials: list[dict] = []
    offset = 0
    while True:
        page = api_get(
            "/executive/officials",
            {"limit": PAGE_LIMIT_OFFICIALS, "offset": offset},
        )
        batch = page.get("officials", [])
        officials.extend(batch)
        offset += PAGE_LIMIT_OFFICIALS
        if len(batch) < PAGE_LIMIT_OFFICIALS or offset >= page.get("total", 0):
            break
    return officials


def fetch_all_trades(politician_id: int) -> list[dict]:
    """Full trade history for one official (executive filings only)."""
    trades: list[dict] = []
    offset = 0
    while True:
        batch = api_get(
            f"/politicians/{politician_id}/trades",
            {"limit": PAGE_LIMIT_TRADES, "offset": offset},
        )
        if not isinstance(batch, list):
            break
        trades.extend(batch)
        if len(batch) < PAGE_LIMIT_TRADES:
            break
        offset += PAGE_LIMIT_TRADES
    # Keep only OGE (executive-branch) filings — cross-branch alumni
    # (e.g. a senator who became a Cabinet secretary) also carry
    # congressional STOCK Act trades, which belong to other datasets.
    return [t for t in trades if (t.get("source") or "") == "oge.gov"]


def normalize_filing_type(form_id: str | None) -> str:
    """Collapse OGE form ids to the two public filing families."""
    if not form_id:
        return ""
    return "278-T" if "T" in form_id.upper() else "278"


def pick_tenure(tenures: list[dict], txn_date: str | None) -> dict:
    """The tenure active on the transaction date (fallback: most recent)."""
    if not tenures:
        return {}
    dated = sorted(tenures, key=lambda t: t.get("start_date") or "")
    if txn_date:
        active = [
            t
            for t in dated
            if (t.get("start_date") or "") <= txn_date
            and (not t.get("end_date") or t["end_date"] >= txn_date)
        ]
        if active:
            return active[-1]
    return dated[-1]


def build_filing_lookups(profile: dict) -> tuple[dict, dict, dict]:
    """From the profile payload build:
    - by_date:   filer_signed_date -> [filing dicts]  (OGE trades carry the
                 filing's signature date as their disclosure_date, so this
                 join is exact by construction)
    - by_doc:    filing_id / source_url -> filing dict (exact override via
                 the profile's recent-trades source_document field)
    - trade_doc: trade id -> source_document
    """
    by_date: dict[str, list[dict]] = {}
    by_doc: dict[str, dict] = {}
    for f in profile.get("filings", []) or []:
        signed = f.get("filer_signed_date") or ""
        if signed:
            by_date.setdefault(signed, []).append(f)
        for k in (f.get("filing_id"), f.get("source_url")):
            if k:
                by_doc[str(k)] = f
    trade_doc: dict[int, str] = {}
    for t in (profile.get("trades") or {}).get("recent", []) or []:
        if t.get("id") is not None and t.get("source_document"):
            trade_doc[t["id"]] = str(t["source_document"])
    return by_date, by_doc, trade_doc


def resolve_filing(
    trade: dict,
    by_date: dict[str, list[dict]],
    by_doc: dict[str, dict],
    trade_doc: dict[int, str],
) -> tuple[str, str]:
    """(filing_type, source_filing_id) for one trade.

    Exact source_document match first; otherwise date-join on the
    filing's signature date. Ambiguous same-day filings are listed
    semicolon-joined rather than guessed.
    """
    doc = trade_doc.get(trade.get("id"))
    if doc:
        f = by_doc.get(doc)
        if f:
            return normalize_filing_type(f.get("form_id")), str(
                f.get("filing_id") or doc
            )
        return "", doc
    matches = by_date.get(trade.get("disclosure_date") or "", [])
    if len(matches) == 1:
        f = matches[0]
        return normalize_filing_type(f.get("form_id")), str(f.get("filing_id") or "")
    if len(matches) > 1:
        types = sorted({normalize_filing_type(f.get("form_id")) for f in matches})
        ids = sorted(str(f.get("filing_id") or "") for f in matches)
        return ";".join(t for t in types if t), ";".join(i for i in ids if i)
    return "", ""


def build_rows(officials: list[dict]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    officials_with_trades = 0
    for off in sorted(officials, key=lambda o: o.get("politician_id") or 0):
        pid = off["politician_id"]
        trades = fetch_all_trades(pid)
        print(f"  {off.get('name')}: {len(trades)} executive trades", flush=True)
        if not trades:
            continue
        officials_with_trades += 1
        profile = api_get(f"/executive/officials/{pid}/profile")
        tenures = profile.get("tenures") or off.get("tenures") or []
        by_date, by_doc, trade_doc = build_filing_lookups(profile)
        for t in trades:
            tenure = pick_tenure(tenures, t.get("transaction_date"))
            filing_type, filing_id = resolve_filing(t, by_date, by_doc, trade_doc)
            rows.append(
                {
                    "filer_name": off.get("name") or "",
                    "role_title": tenure.get("role_title") or "",
                    "agency": tenure.get("department_name")
                    or tenure.get("department_code")
                    or "",
                    "ticker": t.get("ticker") or "",
                    "asset_description": t.get("asset_description") or "",
                    "transaction_type": t.get("trade_type") or "",
                    "transaction_date": t.get("transaction_date") or "",
                    "amount_range": t.get("amount_range") or "",
                    "filing_date": t.get("disclosure_date") or "",
                    "filing_type": filing_type,
                    "source_filing_id": filing_id,
                    "disclosedcapitol_url": f"{SITE_BASE}/politicians/{pid}",
                }
            )
    # Deterministic order: full-row sort so reruns are byte-identical.
    rows.sort(key=lambda r: tuple(r[c] for c in COLUMNS))
    stats = {
        "rows": len(rows),
        "officials_total": len(officials),
        "officials_with_trades": officials_with_trades,
        "tickers": len({r["ticker"] for r in rows if r["ticker"]}),
        "date_min": min((r["transaction_date"] for r in rows if r["transaction_date"]), default=""),
        "date_max": max((r["transaction_date"] for r in rows if r["transaction_date"]), default=""),
        "n_278t": sum(1 for r in rows if r["filing_type"] == "278-T"),
        "n_278": sum(1 for r in rows if r["filing_type"] == "278"),
    }
    return rows, stats


def write_outputs(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    csv_buf = io.StringIO()
    writer = csv.DictWriter(
        csv_buf, fieldnames=COLUMNS, lineterminator="\n", quoting=csv.QUOTE_MINIMAL
    )
    writer.writeheader()
    writer.writerows(rows)
    (DATA_DIR / "executive_trades.csv").write_text(csv_buf.getvalue(), encoding="utf-8")

    # One record per line: line-level diffs stay readable.
    lines = ",\n".join(
        json.dumps({c: r[c] for c in COLUMNS}, ensure_ascii=False) for r in rows
    )
    (DATA_DIR / "executive_trades.json").write_text(
        "[\n" + lines + "\n]\n" if rows else "[]\n", encoding="utf-8"
    )


def update_readme_stats(stats: dict) -> None:
    """Refresh the coverage block between STATS markers in README.md."""
    if not README.exists():
        return
    block = (
        "<!-- STATS:BEGIN (auto-generated by scripts/export.py — do not edit) -->\n"
        f"| Transactions | **{stats['rows']:,}** |\n"
        "|---|---|\n"
        f"| Officials covered | {stats['officials_total']:,} "
        f"({stats['officials_with_trades']:,} with disclosed transactions) |\n"
        f"| Distinct tickers | {stats['tickers']:,} |\n"
        f"| Transaction dates | {stats['date_min']} → {stats['date_max']} |\n"
        f"| Filing mix | {stats['n_278t']:,} × 278-T · {stats['n_278']:,} × 278 |\n"
        f"| Last refreshed | {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (UTC) |\n"
        "<!-- STATS:END -->"
    )
    text = README.read_text(encoding="utf-8")
    new = re.sub(
        r"<!-- STATS:BEGIN.*?STATS:END -->", block, text, count=1, flags=re.DOTALL
    )
    if new != text:
        README.write_text(new, encoding="utf-8")


def main() -> None:
    print(f"Exporting from {API_BASE} ...", flush=True)
    officials = fetch_officials()
    print(f"{len(officials)} executive officials", flush=True)
    rows, stats = build_rows(officials)
    write_outputs(rows)
    update_readme_stats(stats)
    print(
        f"Wrote {stats['rows']:,} transactions "
        f"({stats['officials_with_trades']}/{stats['officials_total']} officials, "
        f"{stats['date_min']} → {stats['date_max']}) "
        f"to {DATA_DIR}/executive_trades.{{csv,json}}"
    )


if __name__ == "__main__":
    main()
