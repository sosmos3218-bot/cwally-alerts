"""스크리너 한 번 실행. 시세와 OpenDART 를 붙이고 판정한다."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from screeners.evaluate import evaluate_company
from screeners.opendart import (
    DEFAULT_CALL_BUDGET,
    DartBudget,
    DartError,
    DartFatal,
    OpenDart,
    api_key_from_env,
    probe_order,
)
from screeners.prices import fetch_naver_ratios, load_listing_quotes, lookup_ratios, naver_quote
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


class CompanySkip(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def prefilter_reason(quote: dict, config: dict) -> str | None:
    """DART 전에 탈락이면 짧은 이유. 통과면 None. PER, PBR 이 없으면 그 항목은 거르지 않는다."""
    name = "".join(str(quote.get("name") or "").split())
    for word in config.get("exclude_financial_keywords") or []:
        if word and word in name:
            return "금융업 제외"
    mcap = quote.get("market_cap_krw")
    low = config.get("mcap_min_krw") or 0
    high = config.get("mcap_max_krw")
    if mcap is None or mcap <= 0:
        return "시가총액이 없음"
    if mcap < low or (high is not None and mcap > high):
        return "시가총액이 기준 밖"
    per_max = float(config["per_max"])
    pbr_max = float(config["pbr_max"])
    per = quote.get("per")
    pbr = quote.get("pbr")
    if per is not None and (per <= 0 or per > per_max):
        return "PER이 기준 밖"
    if pbr is not None and (pbr <= 0 or pbr > pbr_max):
        return "PBR이 기준 밖"
    return None


def discover_report(dart: OpenDart, asof: date) -> tuple[int, str] | None:
    """삼성전자로 시장에 나온 최신 보고서 하나만 고른다. 종목마다 빈 분기를 두드리지 않는다."""
    corp = dart.corp_code("005930")
    if not corp:
        return None
    for year, reprt in probe_order(asof):
        if dart.accounts(corp, year, reprt, "CFS") or dart.accounts(corp, year, reprt, "OFS"):
            return year, reprt
    return None


def _pull_scope(dart: OpenDart, corp: str, year: int, reprt: str) -> list[dict]:
    """연결을 먼저 받고, 별도는 지배주주 현금용으로 항상 시도한다. 013 이면 그 범위만 빈다."""
    rows = list(dart.accounts(corp, year, reprt, "CFS"))
    rows.extend(dart.accounts(corp, year, reprt, "OFS"))
    return rows


def _bundle(dart: OpenDart, corp: str, year: int, reprt: str, rows: list[dict] | None = None) -> list[dict]:
    got = rows if rows is not None else _pull_scope(dart, corp, year, reprt)
    if not got:
        return []
    statements = [{"year": year, "reprt_code": reprt, "rows": got}]
    if reprt != "11011":
        annual = _pull_scope(dart, corp, year - 1, "11011")
        if annual:
            statements.append({"year": year - 1, "reprt_code": "11011", "rows": annual})
    return statements


def statements_for_company(dart: OpenDart, corp: str, asof: date, discovered: tuple[int, str] | None) -> list[dict]:
    if discovered:
        year, reprt = discovered
        bundled = _bundle(dart, corp, year, reprt)
        if bundled or reprt == "11011":
            return bundled
        return _bundle(dart, corp, year - 1, "11011")
    for year, reprt in probe_order(asof)[:4]:
        rows = _pull_scope(dart, corp, year, reprt)
        if rows:
            return _bundle(dart, corp, year, reprt, rows)
    return []


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


def fetch_payload(dart: OpenDart, quote: dict, asof: date, enrich: bool, discovered: tuple[int, str] | None) -> dict:
    corp = dart.corp_code(quote["code"])
    if not corp:
        raise CompanySkip("OpenDART 고유번호 없음")
    statements = statements_for_company(dart, corp, asof, discovered)
    if not statements:
        raise CompanySkip("최근 재무제표 없음")
    year = statements[0]["year"]
    reprt = statements[0]["reprt_code"]
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
        except (DartFatal, DartBudget):
            raise
        except Exception as exc:
            print(f"{quote['code']} 주주환원, 최대주주 조회 실패: {exc}")
            payload["extras"] = "failed"
    return payload


def run_live(
    kind: str,
    codes: list[str] | None,
    limit: int | None,
    all_names: bool,
    out_dir: Path,
    asof: date,
    config_path: Path | None,
    listing=None,
    dart: OpenDart | None = None,
    ratio_fetch=None,
    ratio_cache: Path | None = None,
) -> list[dict]:
    if not codes and limit is None and not all_names:
        raise SystemExit("종목(--codes), 개수(--limit), 전체(--all) 중 하나를 지정하세요. 전체는 OpenDART 호출이 많습니다.")
    config = load_config(kind, config_path)
    if listing is None:
        listing = load_listing_quotes()
    if dart is None and not api_key_from_env():
        raise SystemExit("OPENDART_API_KEY 가 없습니다. 키 값은 출력하지 않습니다. 오프라인은 --demo 또는 selftest 를 쓰세요.")
    chosen = select_quotes(config, codes, None, listing)
    skipped: list[dict] = []
    if not codes:
        kept = []
        for quote in chosen:
            reason = prefilter_reason(quote, config)
            if reason in {"금융업 제외", "시가총액이 없음", "시가총액이 기준 밖"}:
                skipped.append({"code": quote.get("code"), "name": quote.get("name") or "", "reason": reason})
            else:
                kept.append(quote)
        ratios = lookup_ratios(
            [row["code"] for row in kept],
            ratio_fetch or fetch_naver_ratios,
            ratio_cache if ratio_cache is not None else ROOT / "cache" / "ratios.json",
            asof.isoformat(),
        )
        chosen = []
        for quote in kept:
            ratio = ratios.get(quote["code"]) or {}
            quote = dict(quote)
            quote["per"] = ratio.get("per")
            quote["pbr"] = ratio.get("pbr")
            reason = prefilter_reason(quote, config)
            if reason:
                skipped.append({"code": quote.get("code"), "name": quote.get("name") or "", "reason": reason})
            else:
                chosen.append(quote)
        if limit is not None:
            chosen = chosen[:limit]
    print(f"{kind} 재무 조회 대상 {len(chosen)}종목, 시세에서 제외 {len(skipped)}종목, 기준일 {asof.isoformat()}", flush=True)
    if dart is None:
        budget = int(config.get("dart_call_budget") or DEFAULT_CALL_BUDGET)
        dart = OpenDart(api_key_from_env(), ROOT / "cache", max_calls=budget)
    fatal: DartFatal | None = None
    budget_note = ""
    try:
        discovered = discover_report(dart, asof)
    except DartBudget as exc:
        budget_note = str(exc)
        discovered = None
        print(budget_note, flush=True)
        for quote in chosen:
            skipped.append({"code": quote.get("code"), "name": quote.get("name") or "", "reason": "OpenDART 호출 상한"})
    except DartFatal as exc:
        fatal = exc
        discovered = None
        for quote in chosen:
            skipped.append({"code": quote.get("code"), "name": quote.get("name") or "", "reason": "OpenDART 중단"})
    rows = []
    if fatal is None and not budget_note:
        index = 0
        while index < len(chosen):
            quote = chosen[index]
            index += 1
            try:
                payload = fetch_payload(dart, quote, asof, enrich=False, discovered=discovered)
            except DartBudget as exc:
                budget_note = str(exc)
                skipped.append({"code": quote.get("code"), "name": quote.get("name") or "", "reason": "OpenDART 호출 상한"})
                for rest in chosen[index:]:
                    skipped.append({"code": rest.get("code"), "name": rest.get("name") or "", "reason": "OpenDART 호출 상한"})
                break
            except DartFatal as exc:
                fatal = exc
                skipped.append({"code": quote.get("code"), "name": quote.get("name") or "", "reason": "OpenDART 중단"})
                for rest in chosen[index:]:
                    skipped.append({"code": rest.get("code"), "name": rest.get("name") or "", "reason": "OpenDART 중단"})
                break
            except CompanySkip as exc:
                skipped.append({"code": quote.get("code"), "name": quote.get("name") or "", "reason": exc.reason})
                print(f"{quote.get('code')} 건너뜀: {exc.reason}", flush=True)
                continue
            except DartError as exc:
                skipped.append({"code": quote.get("code"), "name": quote.get("name") or "", "reason": str(exc)})
                print(f"{quote.get('code')} 건너뜀: {exc}", flush=True)
                continue
            result = evaluate_company(payload, config)
            if result.get("passes") and not budget_note:
                try:
                    payload = fetch_payload(dart, quote, asof, enrich=True, discovered=discovered)
                    result = evaluate_company(payload, config)
                except DartBudget as exc:
                    budget_note = str(exc)
                except DartFatal as exc:
                    fatal = exc
                except (CompanySkip, DartError) as exc:
                    print(f"{quote.get('code')} 주주 정보 건너뜀: {exc}", flush=True)
            rows.append(result)
            mark = "통과" if result.get("passes") else "탈락"
            print(f"{result['code']} {result['name']} {mark}", flush=True)
            if fatal or budget_note:
                for rest in chosen[index:]:
                    skipped.append({"code": rest.get("code"), "name": rest.get("name") or "", "reason": "OpenDART 호출 상한" if budget_note else "OpenDART 중단"})
                break
    rows = sort_rows(rows)
    stats = {
        "checked": len(rows),
        "skipped": len(skipped),
        "dart_calls": dart.calls,
        "dart_cache_hits": dart.cache_hits,
        "skipped_rows": skipped,
        "budget_note": budget_note,
    }
    write_output(kind, rows, out_dir, stats)
    if fatal is not None:
        print(str(fatal), flush=True)
        raise SystemExit(str(fatal))
    if budget_note:
        print(budget_note, flush=True)
    return rows


def write_output(kind: str, rows: list[dict], out_dir: Path, stats: dict | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(KST)
    extra = stats or {}
    payload = {
        "kind": kind,
        "generated_at": now.isoformat(),
        "count": len(rows),
        "checked": extra.get("checked", len(rows)),
        "skipped": extra.get("skipped", 0),
        "passes": sum(1 for row in rows if row.get("passes")),
        "dart_calls": extra.get("dart_calls"),
        "dart_cache_hits": extra.get("dart_cache_hits"),
        "budget_note": extra.get("budget_note") or "",
        "skipped_rows": extra.get("skipped_rows") or [],
        "rows": rows,
    }
    (out_dir / f"{kind}.json").write_text(dump_json(payload), encoding="utf-8")
    (out_dir / f"{kind}.md").write_text(render_markdown(kind, rows, now), encoding="utf-8")
    calls = payload.get("dart_calls")
    call_text = f", OpenDART 호출 {calls}회" if calls is not None else ""
    print(
        f"{kind} 검사 {payload['checked']}종목, 통과 {payload['passes']}종목, "
        f"건너뜀 {payload['skipped']}종목{call_text}. 파일 {out_dir / (kind + '.json')}"
    )
    if len(rows) <= 40:
        print(render_markdown(kind, rows, now))
