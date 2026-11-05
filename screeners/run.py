"""스크리너 한 번 실행. 시세와 OpenDART 를 붙이고 판정한다."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from screeners.evaluate import evaluate_company
from screeners.opendart import OpenDart, api_key_from_env, probe_order
from screeners.prices import load_listing_quotes, naver_quote
from screeners.render import dump_json, render_markdown
from screeners.universe import is_etf_name, is_preferred_name, is_spac_name, keep_listing_row

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


def load_config(kind: str, path: Path | None = None) -> dict:
    file = path or (ROOT / f"{kind}.json")
    return json.loads(file.read_text(encoding="utf-8"))


def load_fixture(code: str) -> dict:
    file = FIXTURES / f"{code}.json"
    return json.loads(file.read_text(encoding="utf-8"))


def fixture_codes() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.json"))


def sort_rows(rows: list[dict]) -> list[dict]:
    def key(row):
        ratio = (row.get("net_cash") or {}).get("ratio")
        ratio_key = -(ratio if isinstance(ratio, (int, float)) else -1)
        return (0 if row.get("passes") else 1, ratio_key, row.get("code") or "")

    return sorted(rows, key=key)


def screen_fixtures(kind: str, config: dict | None = None) -> list[dict]:
    cfg = config or load_config(kind)
    rows = [evaluate_company(load_fixture(code), cfg) for code in fixture_codes()]
    return sort_rows(rows)


def _asof(text: str | None) -> date:
    if not text:
        return datetime.now(KST).date()
    cleaned = text.strip().replace("-", "")
    return date(int(cleaned[0:4]), int(cleaned[4:6]), int(cleaned[6:8]))


def select_quotes(config: dict, codes: list[str] | None, limit: int | None, listing) -> list[dict]:
    markets = set(config.get("markets") or [])
    if codes:
        wanted = {code.zfill(6) for code in codes}
        by_code = {row["code"]: row for row in listing if row.get("code") in wanted}
        out = []
        for code in wanted:
            row = by_code.get(code)
            if row is None:
                try:
                    row = naver_quote(code)
                except Exception as exc:
                    print(f"{code} 시세 실패: {exc}")
                    continue
            name = row.get("name") or ""
            if is_preferred_name(name) or is_spac_name(name) or is_etf_name(name):
                print(f"{code} {name} 우선주, 스팩, ETF 는 제외")
                continue
            if not row.get("market"):
                row = dict(row)
                row["market"] = "미상"
            out.append(row)
        return out
    rows = [row for row in listing if keep_listing_row(row, markets)]
    rows.sort(key=lambda row: row.get("market_cap_krw") or 0)
    if limit is not None:
        rows = rows[:limit]
    return rows


def fetch_payload(dart: OpenDart, quote: dict, asof: date, enrich: bool) -> dict | None:
    corp = dart.corp_code(quote["code"])
    if not corp:
        print(f"{quote['code']} OpenDART 고유번호 없음")
        return None
    statements = []
    latest = None
    for year, reprt in probe_order(asof):
        rows = dart.accounts(corp, year, reprt)
        if not rows:
            continue
        latest = (year, reprt, rows)
        break
    if latest is None:
        print(f"{quote['code']} 최근 재무제표 없음")
        return None
    year, reprt, rows = latest
    statements.append({"year": year, "reprt_code": reprt, "rows": rows})
    if reprt != "11011":
        annual_rows = dart.accounts(corp, year - 1, "11011")
        if annual_rows:
            statements.append({"year": year - 1, "reprt_code": "11011", "rows": annual_rows})
    payload = {
        "code": quote["code"],
        "name": quote.get("name") or "",
        "market": quote.get("market") or "",
        "market_cap_krw": quote.get("market_cap_krw"),
        "close": quote.get("close"),
        "statements": statements,
        "extras": "skipped",
    }
    if enrich:
        end = asof.strftime("%Y%m%d")
        bgn = (asof - timedelta(days=730)).strftime("%Y%m%d")
        try:
            payload["dividend_rows"] = dart.dividends(corp, year, reprt)
            payload["shareholders"] = dart.shareholders(corp, year, reprt)
            payload["disclosures"] = dart.disclosures(corp, bgn, end)
            payload["extras"] = "checked"
        except Exception as exc:
            print(f"{quote['code']} 주주환원, 최대주주 조회 실패: {exc}")
            payload["extras"] = "failed"
    return payload


def run_live(kind: str, codes: list[str] | None, limit: int | None, all_names: bool, out_dir: Path, asof: date, config_path: Path | None) -> list[dict]:
    if not codes and limit is None and not all_names:
        raise SystemExit("종목(--codes), 개수(--limit), 전체(--all) 중 하나를 지정하세요. 전체는 OpenDART 호출이 많습니다.")
    key = api_key_from_env()
    if not key:
        raise SystemExit("OPENDART_API_KEY 가 없습니다. 키 값은 출력하지 않습니다. 오프라인은 --demo 또는 selftest 를 쓰세요.")
    config = load_config(kind, config_path)
    listing = load_listing_quotes()
    chosen = select_quotes(config, codes, limit, listing)
    print(f"{kind} 시세 {len(chosen)}종목, 기준일 {asof.isoformat()}", flush=True)
    dart = OpenDart(key, ROOT / "cache")
    first_pass = []
    for quote in chosen:
        payload = fetch_payload(dart, quote, asof, enrich=False)
        if payload is None:
            continue
        first_pass.append((quote, payload, evaluate_company(payload, config)))
    rows = []
    for quote, payload, result in first_pass:
        if result.get("passes"):
            enriched = fetch_payload(dart, quote, asof, enrich=True)
            payload = enriched or payload
            result = evaluate_company(payload, config)
        rows.append(result)
        mark = "통과" if result.get("passes") else "탈락"
        print(f"{result['code']} {result['name']} {mark}", flush=True)
    rows = sort_rows(rows)
    write_output(kind, rows, out_dir)
    return rows


def write_output(kind: str, rows: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(KST)
    payload = {
        "kind": kind,
        "generated_at": now.isoformat(),
        "count": len(rows),
        "passes": sum(1 for row in rows if row.get("passes")),
        "rows": rows,
    }
    (out_dir / f"{kind}.json").write_text(dump_json(payload), encoding="utf-8")
    (out_dir / f"{kind}.md").write_text(render_markdown(kind, rows, now), encoding="utf-8")
    print(f"{kind} 검사 {payload['count']}종목, 통과 {payload['passes']}종목. 파일 {out_dir / (kind + '.json')}")
    if len(rows) <= 40:
        print(render_markdown(kind, rows, now))
