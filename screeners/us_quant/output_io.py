"""JSON and CSV writers. Key order is fixed so two runs hash the same."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path

DATA_SOURCES = [
    "SEC EDGAR company_tickers_exchange.json",
    "SEC EDGAR financial statement data set SUB (SIC and form)",
    "SEC EDGAR XBRL frames (share-count prefilter)",
    "SEC EDGAR companyfacts",
    "SEC EDGAR daily index and quarterly master index",
    "SEC EDGAR Form 4 submission text",
    "Yahoo Finance chart daily close",
]

META_COLUMNS = [
    "run_date",
    "trade_date",
    "price_as_of",
    "code_version",
    "min_market_cap",
    "magic_top_n",
    "insider_lookback_days",
    "fscore_basis",
    "fscore_min_score",
    "data_sources",
]


def canon(value):
    if isinstance(value, dict):
        return {key: canon(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canon(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if abs(value) < 1e15 and abs(value - round(value)) < 1e-6:
            return int(round(value))
        return round(value, 8)
    return value


def stamp_rows(rows: list[dict], meta: dict) -> list[dict]:
    prefix = {column: meta.get(column) for column in META_COLUMNS}
    prefix["data_sources"] = "|".join(DATA_SOURCES)
    stamped = []
    for row in rows:
        item = dict(prefix)
        item.update(row)
        stamped.append(canon(item))
    return stamped


def write_table(folder: Path, name: str, meta: dict, rows: list[dict], columns: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"meta": canon(meta), "rows": stamp_rows(rows, meta)}
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (folder / f"{name}.json").write_text(text, encoding="utf-8")
    present = list(columns)
    for row in payload["rows"]:
        for key in row:
            if key not in present:
                present.append(key)
    with (folder / f"{name}.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=present, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in payload["rows"]:
            flat = {}
            for key in present:
                value = row.get(key)
                if isinstance(value, (dict, list)):
                    flat[key] = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                else:
                    flat[key] = "" if value is None else value
            writer.writerow(flat)


def write_meta(folder: Path, meta: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(canon(meta), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (folder / "metadata.json").write_text(text, encoding="utf-8")


def copy_latest(date_folder: Path, latest: Path) -> None:
    if latest.exists():
        shutil.rmtree(latest)
    shutil.copytree(date_folder, latest)


def hash_folder(folder: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    for path in files:
        digest.update(path.relative_to(folder).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def previous_trade_folder(root: Path, trade_date: str) -> Path | None:
    names = []
    if not root.exists():
        return None
    for path in root.iterdir():
        if not path.is_dir() or path.name in {"latest"}:
            continue
        if len(path.name) == 10 and path.name < trade_date and (path / "magic_formula.json").exists():
            names.append(path.name)
    if not names:
        return None
    return root / max(names)


def tickers_in(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for row in payload.get("rows") or []:
        ticker = str(row.get("ticker") or "")
        if ticker:
            out[ticker] = str(row.get("name") or "")
    return out
