"""US screen runner. Network on the first pass, cache on the next."""

from __future__ import annotations

import io
import json
import subprocess
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from screeners.us_quant.assemble import build_fscore_inputs, build_magic_inputs
from screeners.us_quant.edgar_index import dedupe_rows, parse_master, quarter_labels
from screeners.us_quant.facts import EXTRACT_VERSION, extract_companyfacts
from screeners.us_quant.http_util import HttpClient
from screeners.us_quant.insider import aggregate_purchases, parse_form4
from screeners.us_quant.magic import rank_magic
from screeners.us_quant.output_io import (
    DATA_SOURCES,
    copy_latest,
    hash_folder,
    previous_trade_folder,
    tickers_in,
    write_meta,
    write_table,
)
from screeners.us_quant.prices import fetch_close
from screeners.us_quant.universe import foreign_filer, is_common_listing, sic_block_reason

ROOT = Path(__file__).resolve().parents[2]
NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DOMESTIC = {"10-K", "10-K/A", "10-Q", "10-Q/A"}
PERIODIC = DOMESTIC | {"20-F", "20-F/A", "40-F", "40-F/A"}

MAGIC_COLUMNS = [
    "ticker", "cik", "name", "sic", "sic_description", "exchange",
    "price", "price_date", "price_source", "volume",
    "shares", "shares_source", "shares_end", "shares_accession",
    "market_cap", "total_debt", "debt_source", "cash", "cash_source",
    "enterprise_value", "ebit", "ebit_method", "ebit_period_end", "ebit_accession",
    "earnings_yield", "current_assets", "current_liabilities", "net_working_capital",
    "net_fixed_assets", "net_fixed_assets_source", "capital", "roc",
    "ey_rank", "roc_rank", "combined_rank", "balance_sheet_end",
    "fiscal_period", "fiscal_period_end", "filing_accession",
    "multi_class", "ebit_components", "debt_components",
]
FSCORE_COLUMNS = [
    "ticker", "cik", "name", "sic", "sic_description",
    "f_score", "f_roa", "f_cfo", "f_droa", "f_accrual", "f_dlever", "f_dliquid",
    "f_eqoffer", "f_dmargin", "f_dturn", "eq_basis",
    "fiscal_year_end", "prior_year_end", "filing_accession", "prior_filing_accession",
    "inputs", "sources",
]
INSIDER_COLUMNS = [
    "ticker", "cik", "name", "sic",
    "distinct_insiders", "total_value_usd", "roles", "officer_titles",
    "latest_trade_date", "transaction_count", "symbols", "accessions",
]
INTERSECT_COLUMNS = [
    "ticker", "cik", "name", "sic",
    "on_magic", "on_fscore", "f_score", "combined_rank",
    "distinct_insiders", "total_value_usd", "roles", "latest_trade_date",
]
EXCLUDED_COLUMNS = ["ticker", "cik", "name", "sic", "reason", "detail", "fscore_reason"]
PRICE_COLUMNS = ["ticker", "cik", "name", "reason"]
DIFF_COLUMNS = ["list_name", "change", "ticker", "name"]


def load_config(path: Path | None = None) -> dict:
    config_path = path or Path(__file__).resolve().parent / "config.json"
    return json.loads(config_path.read_text(encoding="utf-8"))


def code_version() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    dirty = False
    for line in status.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith("screeners/output/") or path.startswith("screeners/cache/"):
            continue
        dirty = True
        break
    return f"{sha}-dirty" if dirty else sha


def cik_key(value) -> str:
    return str(int(value)).zfill(10)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(as_of: date, run_date: date, config: dict, out_root: Path, cache: Path) -> dict:
    started = datetime.now(KST)
    client = HttpClient(float(config.get("sec_max_per_second") or 7))
    cache.mkdir(parents=True, exist_ok=True)
    phases = {}

    def phase(name, fn):
        t0 = datetime.now(KST)
        value = fn()
        phases[name] = round((datetime.now(KST) - t0).total_seconds(), 3)
        print(f"PHASE {name} {phases[name]}s", flush=True)
        return value

    listings = phase("tickers", lambda: _listings(client, cache, config))
    shares = phase("share_frames", lambda: _share_frames(client, cache, config))
    prices, failures = phase("prices", lambda: _prices(cache, listings, shares, as_of))
    sic_map = phase("sic", lambda: _sic_map(client, cache, config))
    candidates, price_rows = _candidates(listings, shares, prices, failures, sic_map, config, client, cache)
    print(f"CANDIDATES {len(candidates)} price_failures {len(price_rows)}", flush=True)
    index_rows, filings_through = phase("index", lambda: _index(client, cache, as_of, int(config["insider"]["lookback_days"])))
    facts_map, fact_errors = phase("facts", lambda: _facts(client, cache, candidates, index_rows))
    screened = _screen(candidates, facts_map, fact_errors, as_of, config)
    insider_rows = phase("insider", lambda: _insiders(client, cache, index_rows, screened["universe_ciks"], as_of, int(config["insider"]["lookback_days"]), screened["by_cik"]))
    trade_s = as_of.isoformat()
    meta = _meta(config, run_date, as_of, screened, filings_through, phases)
    folder = out_root / trade_s
    _emit(folder, meta, screened, insider_rows, price_rows, out_root, trade_s)
    digest = hash_folder(folder)
    elapsed = round((datetime.now(KST) - started).total_seconds(), 3)
    print(f"WROTE {folder} sha256 {digest} elapsed_sec {elapsed}", flush=True)
    return {"folder": str(folder), "sha256": digest, "elapsed_sec": elapsed, "phases": phases, "counts": meta["counts"]}


def _listings(client: HttpClient, cache: Path, config: dict) -> list[dict]:
    path = cache / "company_tickers_exchange.json"
    if path.exists():
        payload = _read_json(path)
    else:
        raw = client.get("https://www.sec.gov/files/company_tickers_exchange.json")
        if not raw:
            raise RuntimeError("ticker list unavailable")
        payload = json.loads(raw)
        _write_json(path, payload)
    fields = payload["fields"]
    rows = []
    for raw in payload["data"]:
        row = dict(zip(fields, raw))
        if is_common_listing(row, list(config["exchanges"])):
            rows.append(row)
    return rows


def _share_frames(client: HttpClient, cache: Path, config: dict) -> dict[str, dict]:
    path = cache / "share_frames.json"
    periods = list(config["share_frame_periods"])
    if path.exists():
        cached = _read_json(path)
        if cached.get("periods") == periods:
            return cached["shares"]
    shares: dict[str, dict] = {}

    def consider(cik: str, end: str, val: float, accn: str, source: str, rank: int) -> None:
        if not val or val <= 0:
            return
        current = shares.get(cik)
        item = {"end": end, "val": val, "accn": accn, "source": source, "rank": rank}
        if current is None or (end, rank) > (current["end"], current["rank"]):
            shares[cik] = item

    jobs = []
    for period in periods:
        jobs.append(("dei", "EntityCommonStockSharesOutstanding", f"{period}I", 2))
        jobs.append(("us-gaap", "CommonStockSharesOutstanding", f"{period}I", 1))
        jobs.append(("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", period, 0))
    for taxonomy, tag, frame, rank in jobs:
        url = f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/shares/{frame}.json"
        raw = client.get(url, missing_ok=True)
        if not raw:
            print(f"FRAME missing {frame} {tag}", flush=True)
            continue
        payload = json.loads(raw)
        for point in payload.get("data") or []:
            consider(cik_key(point["cik"]), str(point.get("end") or ""), float(point.get("val") or 0), str(point.get("accn") or ""), f"{taxonomy}:{tag}", rank)
    _write_json(path, {"periods": periods, "shares": shares})
    return shares


def _prices(cache: Path, listings: list[dict], shares: dict, as_of: date):
    needed = sorted({row["ticker"] for row in listings if cik_key(row["cik"]) in shares})
    path = cache / "prices" / f"{as_of.isoformat()}.json"
    stored = _read_json(path) if path.exists() else {}
    missing = [symbol for symbol in needed if symbol not in stored]

    def one(symbol: str):
        try:
            quote = fetch_close(symbol, as_of)
        except Exception as exc:  # noqa: BLE001
            return symbol, {"error": "price_unavailable", "detail": type(exc).__name__}
        if quote is None:
            return symbol, {"error": "price_unavailable"}
        return symbol, quote

        if missing:
        print(f"PRICE fetch {len(missing)}", flush=True)
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(one, symbol) for symbol in missing]
            done = 0
            for future in as_completed(futures):
                symbol, quote = future.result()
                stored[symbol] = quote
                done += 1
                if done % 500 == 0:
                    print(f"PRICE {done}/{len(missing)}", flush=True)
                    _write_json(path, stored)
        _write_json(path, stored)
        stored = _read_json(path)
    failures = []
    good = {}
    for symbol in needed:
        quote = stored.get(symbol) or {"error": "price_unavailable"}
        if quote.get("error"):
            failures.append(symbol)
        else:
            good[symbol] = quote
    return good, failures


def _sic_map(client: HttpClient, cache: Path, config: dict) -> dict[str, dict]:
    quarters = list(config["sic_dataset_quarters"])
    path = cache / "sic_map.json"
    if path.exists():
        cached = _read_json(path)
        if cached.get("quarters") == quarters:
            return cached["issuers"]
    issuers: dict[str, dict] = {}
    for quarter in quarters:
        url = f"https://www.sec.gov/files/dera/data/financial-statement-data-sets/{quarter}.zip"
        raw = client.get(url, timeout=180, missing_ok=True)
        if not raw:
            print(f"SIC zip missing {quarter}", flush=True)
            continue
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            with archive.open("sub.txt") as handle:
                _merge_sub(handle.read().decode("utf-8", errors="replace"), issuers)
        print(f"SIC {quarter} issuers {len(issuers)}", flush=True)
    _write_json(path, {"quarters": quarters, "issuers": issuers})
    return issuers


def _merge_sub(text: str, issuers: dict[str, dict]) -> None:
    lines = text.splitlines()
    if not lines:
        return
    header = lines[0].split("\t")
    idx = {name: pos for pos, name in enumerate(header)}
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) < len(header):
            continue
        cik = cik_key(cols[idx["cik"]])
        filed = cols[idx["filed"]][:10]
        form = cols[idx["form"]].strip().upper()
        sic_raw = cols[idx["sic"]].strip()
        name = cols[idx["name"]].strip()
        slot = issuers.setdefault(cik, {"sic": None, "sic_description": None, "forms": [], "name": name, "filed": ""})
        if form and form not in slot["forms"]:
            slot["forms"].append(form)
        if filed >= slot["filed"]:
            slot["filed"] = filed
            slot["name"] = name or slot["name"]
            if sic_raw.isdigit():
                slot["sic"] = int(sic_raw)


def _submissions(client: HttpClient, cache: Path, cik: str) -> dict | None:
    path = cache / "submissions" / f"{cik}.json"
    if path.exists():
        return _read_json(path)
    raw = client.get(f"https://data.sec.gov/submissions/CIK{cik}.json", missing_ok=True)
    if not raw:
        return None
    payload = json.loads(raw)
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = sorted({str(item).upper() for item in recent.get("form") or []})
    sic_raw = str(payload.get("sic") or "")
    record = {
        "sic": int(sic_raw) if sic_raw.isdigit() else None,
        "sic_description": payload.get("sicDescription") or None,
        "forms": forms,
        "name": payload.get("name") or "",
        "filed": "",
    }
    _write_json(path, record)
    return record


def _candidates(listings, shares, prices, failures, sic_map, config, client, cache):
    by_cik = defaultdict(list)
    for row in listings:
        by_cik[cik_key(row["cik"])].append(row)
    fail_set = set(failures)
    price_rows = []
    for symbol in sorted(fail_set):
        owner = next((row for row in listings if row["ticker"] == symbol), None)
        price_rows.append(
            {
                "ticker": symbol,
                "cik": "" if owner is None else cik_key(owner["cik"]),
                "name": "" if owner is None else owner.get("name") or "",
                "reason": "price_unavailable",
            }
        )
    floor = float(config["min_market_cap"])
    ratio = float(config["market_cap_prefilter_ratio"])
    pending = []
    for cik, rows in by_cik.items():
        if cik not in shares:
            continue
        quotes = []
        for row in rows:
            quote = prices.get(row["ticker"])
            if quote is None:
                continue
            quotes.append((row, quote))
        if not quotes:
            continue
        share = shares[cik]
        best_price = max(float(quote["price"]) for _, quote in quotes)
        estimated = best_price * float(share["val"])
        if estimated < floor * ratio:
            continue
        pending.append((cik, rows, quotes, share, estimated))

    resolved = []
    need_sub = [cik for cik, *_ in pending if cik not in sic_map or not (sic_map[cik].get("forms") or [])]

    def load_sub(cik: str):
        return cik, _submissions(client, cache, cik)

    extra = {}
    if need_sub:
        print(f"SUBMISSIONS {len(need_sub)}", flush=True)
        with ThreadPoolExecutor(max_workers=6) as pool:
            for future in as_completed([pool.submit(load_sub, cik) for cik in need_sub]):
                cik, record = future.result()
                if record:
                    extra[cik] = record
    for cik, rows, quotes, share, estimated in pending:
        info = dict(sic_map.get(cik) or {})
        if cik in extra:
            info = _prefer_sic(info, extra[cik])
        sic = info.get("sic")
        forms = set(info.get("forms") or [])
        if sic is None and not forms:
            # Still unknown. Keep it and let companyfacts show the forms.
            pass
        elif sic_block_reason(sic, config["sic_exclude"]):
            continue
        elif forms and foreign_filer(forms, config["foreign_forms"], config["domestic_forms"]):
            continue
        elif forms and not (forms & set(config["domestic_forms"])) and (forms & set(config["foreign_forms"])):
            continue
        quotes_sorted = sorted(quotes, key=lambda item: (-(float(item[1]["price"]) * float(item[1].get("volume") or 0)), item[0]["ticker"]))
        primary = quotes_sorted[0]
        resolved.append(
            {
                "cik": cik,
                "name": (info.get("name") or primary[0].get("name") or "").strip(),
                "sic": sic,
                "sic_description": info.get("sic_description"),
                "exchange": primary[0].get("exchange"),
                "tickers": [
                    {
                        "ticker": row["ticker"],
                        "name": row.get("name") or "",
                        "price": float(quote["price"]),
                        "price_date": quote["price_date"],
                        "volume": int(quote.get("volume") or 0),
                        "price_source": quote["price_source"],
                    }
                    for row, quote in sorted(quotes, key=lambda item: item[0]["ticker"])
                ],
                "primary": primary[0]["ticker"],
                "multi_class": len(quotes) > 1,
                "prefilter_shares": float(share["val"]),
                "prefilter_share_source": share["source"],
                "prefilter_mcap": estimated,
                "forms": sorted(forms),
            }
        )
    resolved.sort(key=lambda item: item["cik"])
    return resolved, price_rows


def _prefer_sic(base: dict, extra: dict) -> dict:
    merged = dict(base)
    forms = set(base.get("forms") or []) | set(extra.get("forms") or [])
    merged["forms"] = sorted(forms)
    if merged.get("sic") is None and extra.get("sic") is not None:
        merged["sic"] = extra["sic"]
    if not merged.get("sic_description") and extra.get("sic_description"):
        merged["sic_description"] = extra["sic_description"]
    if extra.get("name") and not merged.get("name"):
        merged["name"] = extra["name"]
    return merged


def _index(client: HttpClient, cache: Path, as_of: date, lookback_days: int):
    start = as_of - timedelta(days=lookback_days + 7)
    rows = []
    received = None
    for year, quarter in quarter_labels(start, as_of):
        url = f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/master.idx"
        path = cache / "index" / f"{year}-QTR{quarter}-master.idx"
        if path.exists():
            text = path.read_bytes()
        else:
            text = client.get(url, timeout=120, missing_ok=True)
            if text:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text)
        if not text:
            continue
        parsed, stamp = parse_master(text)
        rows.extend(parsed)
        if stamp and (received is None or stamp > received):
            received = stamp
    # Days after the compiled master, when that daily file exists.
    cursor = (received + timedelta(days=1)) if received else start
    while cursor <= as_of:
        if cursor.weekday() < 5:
            ymd = cursor.strftime("%Y%m%d")
            quarter = (cursor.month - 1) // 3 + 1
            url = f"https://www.sec.gov/Archives/edgar/daily-index/{cursor.year}/QTR{quarter}/master.{ymd}.idx"
            path = cache / "index" / f"master.{ymd}.idx"
            if path.exists():
                text = path.read_bytes()
            else:
                text = client.get(url, timeout=60, missing_ok=True)
                if text:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(text)
            if text:
                parsed, _stamp = parse_master(text)
                rows.extend(parsed)
        cursor += timedelta(days=1)
    rows = dedupe_rows(rows)
    filed = [row["filed"] for row in rows if row["filed"] <= as_of.isoformat()]
    through = max(filed) if filed else (received.isoformat() if received else None)
    print(f"INDEX rows {len(rows)} filings_through {through}", flush=True)
    return rows, through


def _facts(client: HttpClient, cache: Path, candidates: list[dict], index_rows: list[dict]):
    fresh = defaultdict(set)
    for row in index_rows:
        if row["form"] in PERIODIC:
            fresh[row["cik"]].add(row["accn"])
    out = {}
    errors = {}

    def one(cik: str):
        path = cache / "facts" / f"{cik}.json"
        if path.exists():
            cached = _read_json(path)
            seen = set(cached.get("seen_accn") or [])
            if cached.get("extract_version") == EXTRACT_VERSION and fresh.get(cik, set()) <= seen:
                return cik, cached, None
        raw = client.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", timeout=90, missing_ok=True)
        if not raw:
            return cik, None, "companyfacts_unavailable"
        try:
            extracted = extract_companyfacts(json.loads(raw))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return cik, None, f"facts_parse_failed:{type(exc).__name__}"
        _write_json(path, extracted)
        return cik, extracted, None

    print(f"FACTS {len(candidates)}", flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(one, item["cik"]) for item in candidates]
        done = 0
        for future in as_completed(futures):
            cik, extracted, error = future.result()
            done += 1
            if error or extracted is None:
                errors[cik] = error or "companyfacts_unavailable"
            else:
                out[cik] = extracted
            if done % 200 == 0:
                print(f"FACTS {done}/{len(candidates)}", flush=True)
    print(f"FACTS ok {len(out)} errors {len(errors)}", flush=True)
    return out, errors


def _screen(candidates, facts_map, fact_errors, as_of: date, config: dict) -> dict:
    floor = float(config["min_market_cap"])
    limits = config["staleness"]
    min_score = int(config["fscore"]["min_score"])
    if config["fscore"]["basis"] != "annual":
        raise RuntimeError("fscore basis other than annual is not implemented")
    if config["magic"]["ebit_basis"] != "ttm":
        raise RuntimeError("magic ebit basis other than ttm is not implemented")
    magic_ready = []
    fscore_ready = []
    excluded = []
    universe = {}
    fscore_skips = Counter()
    for item in candidates:
        cik = item["cik"]
        if cik in fact_errors:
            excluded.append(_drop(item, fact_errors[cik], ""))
            continue
        facts = facts_map.get(cik)
        if facts is None:
            excluded.append(_drop(item, "companyfacts_unavailable", ""))
            continue
        forms = set(facts.get("forms_seen") or []) | set(item.get("forms") or [])
        if foreign_filer(forms, config["foreign_forms"], config["domestic_forms"]):
            continue
        if forms and not (forms & DOMESTIC):
            continue
        sic = item.get("sic")
        blocked = sic_block_reason(sic, config["sic_exclude"])
        if blocked:
            continue
        primary = next(row for row in item["tickers"] if row["ticker"] == item["primary"])
        price = float(primary["price"])
        if (as_of - date.fromisoformat(primary["price_date"])).days > int(limits["max_price_age_days"]):
            excluded.append(_drop(item, "stale_price", primary["price_date"]))
            continue
        magic = None
        if item["multi_class"]:
            magic_reason = "multi_class_shares_unsplit"
            magic_detail = ",".join(row["ticker"] for row in item["tickers"])
        else:
            magic = build_magic_inputs(facts, as_of, price, limits)
            magic_reason = "" if magic.get("ok") else magic.get("reason") or "magic_incomplete"
            magic_detail = "" if magic.get("ok") else str(magic.get("detail") or "")
        if magic and magic.get("ok") and float(magic["market_cap"]) < floor:
            continue
        if magic_reason == "shares_outstanding_missing" and item["prefilter_mcap"] < floor:
            continue
        if not magic_reason and magic and float(magic["market_cap"]) >= floor:
            in_universe = True
        elif item["prefilter_mcap"] >= floor or (magic and magic.get("ok")):
            in_universe = True
        else:
            in_universe = False
        if not in_universe:
            continue
        fscore = build_fscore_inputs(facts, as_of, int(limits["max_annual_age_days"]))
        fscore_reason = "" if fscore.get("ok") else fscore.get("reason") or "fscore_incomplete"
        if fscore_reason:
            fscore_skips[fscore_reason] += 1
        if magic_reason:
            excluded.append(_drop(item, magic_reason, magic_detail, fscore_reason))
        base = {
            "cik": cik,
            "name": item["name"] or (facts.get("entity_name") or ""),
            "sic": sic,
            "sic_description": item.get("sic_description"),
            "exchange": item.get("exchange"),
            "primary": item["primary"],
            "quote": primary,
        }
        universe[cik] = base
        if magic and magic.get("ok"):
            magic_ready.append(_magic_public(base, magic, item["multi_class"]))
        if fscore.get("ok"):
            fscore_ready.append(_fscore_public(base, fscore))
    ranked = rank_magic(magic_ready, int(config["magic"]["top_n"]))
    high = [row for row in fscore_ready if int(row["f_score"]) >= min_score]
    high.sort(key=lambda row: (-int(row["f_score"]), row["ticker"]))
    excluded.sort(key=lambda row: (row["reason"], row["ticker"]))
    return {
        "magic": ranked,
        "fscore": high,
        "excluded": excluded,
        "universe_ciks": set(universe),
        "by_cik": universe,
        "fscore_skip_reasons": dict(fscore_skips),
        "magic_ranked_count": len(magic_ready),
        "fscore_scored_count": len(fscore_ready),
    }


def _drop(item: dict, reason: str, detail: str, fscore_reason: str = "") -> dict:
    return {
        "ticker": item["primary"],
        "cik": item["cik"],
        "name": item["name"],
        "sic": item.get("sic"),
        "reason": reason,
        "detail": detail,
        "fscore_reason": fscore_reason,
    }


def _fiscal_period(magic: dict) -> str:
    for component in magic.get("ebit_components") or []:
        if component.get("end") == magic.get("ebit_period_end") and component.get("fp"):
            fy = component.get("fy")
            return f"{fy}{component['fp']}" if fy else str(component["fp"])
    return str(magic.get("ebit_period_end") or "")


def _magic_public(base: dict, magic: dict, multi: bool) -> dict:
    quote = base["quote"]
    shares = magic["shares_component"]
    return {
        "ticker": base["primary"],
        "cik": base["cik"],
        "name": base["name"],
        "sic": base["sic"],
        "sic_description": base["sic_description"],
        "exchange": base["exchange"],
        "price": round(float(quote["price"]), 4),
        "price_date": quote["price_date"],
        "price_source": quote["price_source"],
        "volume": int(quote["volume"]),
        "shares": magic["shares"],
        "shares_source": magic["shares_source"],
        "shares_end": shares.get("end"),
        "shares_accession": shares.get("accn"),
        "market_cap": magic["market_cap"],
        "total_debt": magic["total_debt"],
        "debt_source": magic["debt_source"],
        "cash": magic["cash"],
        "cash_source": magic["cash_source"],
        "enterprise_value": magic["enterprise_value"],
        "ebit": magic["ebit"],
        "ebit_method": magic["ebit_method"],
        "ebit_period_end": magic["ebit_period_end"],
        "ebit_accession": magic["ebit_accession"],
        "earnings_yield": magic["earnings_yield"],
        "current_assets": magic["current_assets"],
        "current_liabilities": magic["current_liabilities"],
        "net_working_capital": magic["net_working_capital"],
        "net_fixed_assets": magic["net_fixed_assets"],
        "net_fixed_assets_source": magic["net_fixed_assets_source"],
        "capital": magic["capital"],
        "roc": magic["roc"],
        "balance_sheet_end": magic["balance_sheet_end"],
        "fiscal_period": _fiscal_period(magic),
        "fiscal_period_end": magic["fiscal_period_end"],
        "filing_accession": magic["filing_accession"],
        "multi_class": multi,
        "ebit_components": magic["ebit_components"],
        "debt_components": magic["debt_components"],
    }


def _fscore_public(base: dict, scored: dict) -> dict:
    row = {
        "ticker": base["primary"],
        "cik": base["cik"],
        "name": base["name"],
        "sic": base["sic"],
        "sic_description": base["sic_description"],
        "f_score": scored["f_score"],
        "eq_basis": scored["eq_basis"],
        "fiscal_year_end": scored["fiscal_year_end"],
        "prior_year_end": scored["prior_year_end"],
        "filing_accession": scored["filing_accession"],
        "prior_filing_accession": scored["prior_filing_accession"],
        "inputs": scored["inputs"],
        "sources": scored["sources"],
    }
    row.update(scored["flags"])
    return row


def _insiders(client, cache, index_rows, universe: set[str], as_of: date, lookback_days: int, by_cik: dict):
    start = (as_of - timedelta(days=lookback_days + 7)).isoformat()
    end = as_of.isoformat()
    wanted = [
        row
        for row in index_rows
        if row["form"] in {"4", "4/A"} and start <= row["filed"] <= end and row["cik"] in universe
    ]
    print(f"FORM4 files {len(wanted)}", flush=True)

    def one(row: dict):
        path = cache / "form4" / f"{row['accn']}.json"
        if path.exists():
            trades = _read_json(path)
            return trades
        raw = client.get("https://www.sec.gov/Archives/" + row["filename"], timeout=60, missing_ok=True)
        if not raw:
            return []
        try:
            trades = parse_form4(raw.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            trades = []
        for trade in trades:
            trade["accn"] = row["accn"]
            trade["filed"] = row["filed"]
        _write_json(path, trades)
        return trades

    trades = []
    if wanted:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(one, row) for row in wanted]
            done = 0
            for future in as_completed(futures):
                trades.extend(future.result() or [])
                done += 1
                if done % 400 == 0:
                    print(f"FORM4 {done}/{len(wanted)}", flush=True)
    grouped = aggregate_purchases(trades, as_of, lookback_days)
    rows = []
    for cik, bucket in grouped.items():
        base = by_cik.get(cik)
        if base is None:
            continue
        rows.append(
            {
                "ticker": base["primary"],
                "cik": cik,
                "name": base["name"],
                "sic": base["sic"],
                "distinct_insiders": bucket["distinct_insiders"],
                "total_value_usd": bucket["total_value_usd"],
                "roles": bucket["roles"],
                "officer_titles": bucket["officer_titles"],
                "latest_trade_date": bucket["latest_trade_date"],
                "transaction_count": bucket["transaction_count"],
                "symbols": bucket["symbols"],
                "accessions": bucket["accessions"],
            }
        )
    rows.sort(key=lambda row: (-float(row["total_value_usd"]), row["ticker"]))
    return rows


def _meta(config, run_date, as_of, screened, filings_through, phases) -> dict:
    del phases
    dates = [row["price_date"] for row in screened["magic"]]
    price_as_of = as_of.isoformat()
    if dates:
        counts = Counter(dates)
        price_as_of = sorted(counts, key=lambda item: (-counts[item], item))[0]
    return {
        "run_date": run_date.isoformat(),
        "trade_date": as_of.isoformat(),
        "price_as_of": price_as_of,
        "code_version": code_version(),
        "min_market_cap": config["min_market_cap"],
        "magic_top_n": config["magic"]["top_n"],
        "insider_lookback_days": config["insider"]["lookback_days"],
        "fscore_basis": config["fscore"]["basis"],
        "fscore_min_score": config["fscore"]["min_score"],
        "data_sources": DATA_SOURCES,
        "insider_filings_through": filings_through,
        "config": config,
        "counts": {
            "magic_input_rows": screened["magic_ranked_count"],
            "magic_top": len(screened["magic"]),
            "fscore_scored": screened["fscore_scored_count"],
            "fscore_listed": len(screened["fscore"]),
            "universe": len(screened["universe_ciks"]),
            "excluded": len(screened["excluded"]),
            "fscore_skip_reasons": screened["fscore_skip_reasons"],
        },
    }


def _emit(folder, meta, screened, insider_rows, price_rows, out_root, trade_s):
    # phases differ between a cold run and a warm run. Keep them out of the hashed files.
    public = dict(meta)
    magic_ids = {row["ticker"] for row in screened["magic"]}
    f_ids = {row["ticker"]: row for row in screened["fscore"]}
    rank_of = {row["ticker"]: row.get("combined_rank") for row in screened["magic"]}
    # combined_rank is added by rank_magic only on the returned top slice.
    # Re-read from the row.
    insider_ids = {row["ticker"]: row for row in insider_rows}
    intersection = []
    for ticker in sorted(set(magic_ids) | set(f_ids)):
        if ticker not in insider_ids:
            continue
        left = insider_ids[ticker]
        scored = f_ids.get(ticker) or {}
        magic_row = next((row for row in screened["magic"] if row["ticker"] == ticker), None)
        intersection.append(
            {
                "ticker": ticker,
                "cik": left["cik"],
                "name": left["name"],
                "sic": left["sic"],
                "on_magic": ticker in magic_ids,
                "on_fscore": ticker in f_ids,
                "f_score": scored.get("f_score"),
                "combined_rank": None if magic_row is None else magic_row.get("combined_rank"),
                "distinct_insiders": left["distinct_insiders"],
                "total_value_usd": left["total_value_usd"],
                "roles": left["roles"],
                "latest_trade_date": left["latest_trade_date"],
            }
        )
    previous = previous_trade_folder(out_root, trade_s)
    diff_rows = _diff(previous, screened["magic"], screened["fscore"], insider_rows, intersection)
    if previous is not None:
        public["previous_trade_date"] = previous.name
    else:
        public["previous_trade_date"] = None
    public["counts"] = dict(public["counts"])
    public["counts"]["insider"] = len(insider_rows)
    public["counts"]["intersection"] = len(intersection)
    public["counts"]["price_failures"] = len(price_rows)
    write_meta(folder, public)
    write_table(folder, "magic_formula", public, screened["magic"], MAGIC_COLUMNS)
    write_table(folder, "fscore", public, screened["fscore"], FSCORE_COLUMNS)
    write_table(folder, "insider_buying", public, insider_rows, INSIDER_COLUMNS)
    write_table(folder, "intersection", public, intersection, INTERSECT_COLUMNS)
    write_table(folder, "excluded", public, screened["excluded"], EXCLUDED_COLUMNS)
    write_table(folder, "price_failures", public, price_rows, PRICE_COLUMNS)
    write_table(folder, "diff", public, diff_rows, DIFF_COLUMNS)
    copy_latest(folder, out_root / "latest")


def _diff(previous: Path | None, magic, fscore, insider, intersection) -> list[dict]:
    current = {
        "magic_formula": {row["ticker"]: row.get("name") or "" for row in magic},
        "fscore": {row["ticker"]: row.get("name") or "" for row in fscore},
        "insider_buying": {row["ticker"]: row.get("name") or "" for row in insider},
        "intersection": {row["ticker"]: row.get("name") or "" for row in intersection},
    }
    prior = {}
    if previous is not None:
        for name in current:
            prior[name] = tickers_in(previous / f"{name}.json")
    rows = []
    for name, now in current.items():
        old = prior.get(name) or {}
        for ticker in sorted(set(now) - set(old)):
            rows.append({"list_name": name, "change": "entered", "ticker": ticker, "name": now[ticker]})
        for ticker in sorted(set(old) - set(now)):
            rows.append({"list_name": name, "change": "exited", "ticker": ticker, "name": old[ticker]})
    return rows


def default_run_date() -> date:
    return datetime.now(KST).date()


def default_as_of() -> date:
    day = datetime.now(NY).date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day
