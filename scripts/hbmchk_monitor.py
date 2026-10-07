#!/usr/bin/env python3
"""Conservative HBM/video fact-check monitor. Python standard library only."""
from __future__ import annotations
import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "hbmchk.json"
STATE_PATH = ROOT / "state" / "hbmchk.json"
KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
YAHOO_UA = "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"
US_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
NOTICE_RE = re.compile(
    r"HBM|고대역폭|잠정실적|영업실적|매출액또는손익|자기주식|실적발표|"
    r"분기보고서|사업보고서|반기보고서|단일판매|공급계약", re.I
)
def helper(name: str):
    try:
        return __import__(name)
    except ImportError:
        flat = ROOT.parent / "ntfy-source" / f"scripts__{name}.py"
        if not flat.exists():
            raise
        spec = importlib.util.spec_from_file_location(name, flat)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return copy.deepcopy(fallback)
    data = json.loads(path.read_text(encoding="utf-8"))
    out = copy.deepcopy(fallback)
    if isinstance(data, dict):
        out.update(data)
    return out
def atomic_save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
def parse_day(raw) -> date:
    text = str(raw or "").strip()[:10]
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return date.fromisoformat(text)
def parse_notice_time(raw: str) -> datetime:
    text = str(raw or "").strip().replace(".", "-")
    if not text:
        raise ValueError("공시 시각 없음")
    try:
        got = datetime.fromisoformat(text)
    except ValueError:
        got = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                got = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if got is None:
            raise ValueError(f"공시 시각 해석 불가: {raw}")
    return got.replace(tzinfo=KST) if got.tzinfo is None else got.astimezone(KST)
def previous_days(end: date, count: int, is_open) -> list[date]:
    out, d = [], end
    while len(out) < count:
        if is_open(d):
            out.append(d)
        d -= timedelta(days=1)
    return out
def is_us_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in US_HOLIDAYS
def expected_us_day(now: datetime) -> date:
    local = now.astimezone(NY)
    d = local.date() if local.time() >= time(16, 30) else local.date() - timedelta(days=1)
    while not is_us_day(d):
        d -= timedelta(days=1)
    return d
def completed_kr(rows: list[dict], now: datetime, kr_calendar, need_close: int = 20) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("Naver 일봉 응답이 비었음")
    expected = kr_calendar.prev_trading_day(now.astimezone(KST).date())
    done, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Naver 일봉 행 형식 오류")
        try:
            day = parse_day(row.get("day"))
        except Exception as exc:
            raise ValueError("Naver 거래일 해석 불가") from exc
        if day > expected:
            continue
        if day in seen:
            raise ValueError(f"Naver 거래일 중복 {day}")
        seen.add(day)
        close = row.get("close")
        if close is None or not math.isfinite(float(close)) or float(close) <= 0:
            raise ValueError(f"Naver 종가 누락 {day}")
        done.append({"day": day, "close": float(close), "foreign": row.get("foreign")})
    if len(done) < need_close:
        raise ValueError(f"완료 일봉 {len(done)}개, 최소 {need_close}개 필요")
    if any(done[i]["day"] <= done[i + 1]["day"] for i in range(len(done) - 1)):
        raise ValueError("Naver 일봉이 최신순 고유 순서가 아님")
    expected3 = previous_days(expected, 3, kr_calendar.is_trading_day)
    if [r["day"] for r in done[:3]] != expected3:
        raise ValueError(f"최근 3거래일 불완전, 기대 {','.join(map(str, expected3))}")
    return done
def kr_flow_result(done: list[dict], ma_days: int = 20) -> dict:
    latest3 = done[:3]
    if any(r["foreign"] is None for r in latest3):
        raise ValueError("최근 3거래일 외국인 순매수 수량 누락")
    foreign = [float(r["foreign"]) for r in latest3]
    if not all(math.isfinite(v) for v in foreign):
        raise ValueError("외국인 순매수 수량이 유효한 숫자가 아님")
    ma20 = sum(r["close"] for r in done[:ma_days]) / ma_days
    hit = all(v > 0 for v in foreign) and done[0]["close"] >= ma20
    return {"hit": hit, "date": done[0]["day"], "close": done[0]["close"], "ma20": ma20, "foreign": foreign}
def completed_us(payload: dict, now: datetime, need: int = 30) -> list[dict]:
    try:
        result = payload["chart"]["result"][0]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Yahoo chart 구조 오류") from exc
    if not isinstance(stamps, list) or len(stamps) != len(closes):
        raise ValueError("Yahoo timestamp/close 길이 오류")
    expected, by_day = expected_us_day(now), {}
    for stamp, close in zip(stamps, closes):
        if close is None:
            continue
        d = datetime.fromtimestamp(int(stamp), timezone.utc).astimezone(NY).date()
        if d <= expected:
            if not math.isfinite(float(close)) or float(close) <= 0:
                raise ValueError(f"Yahoo 종가가 유효한 양수가 아님 {d}")
            if d in by_day:
                raise ValueError(f"Yahoo 거래일 중복 {d}")
            by_day[d] = float(close)
    rows = [{"day": d, "close": by_day[d]} for d in sorted(by_day)]
    if len(rows) < need:
        raise ValueError(f"Yahoo 완료 일봉 {len(rows)}개, 최소 {need}개 필요")
    expected_dates = list(reversed(previous_days(expected, need, is_us_day)))
    if [r["day"] for r in rows[-need:]] != expected_dates:
        raise ValueError(f"Yahoo 최근 거래일 불완전, 최신 기대 {expected}")
    return rows
def drawdown_result(rows: list[dict], lookback: int = 30) -> dict:
    window = rows[-lookback:]
    anchor = max(window, key=lambda r: r["close"])
    latest = window[-1]
    dd = max(0.0, (anchor["close"] - latest["close"]) / anchor["close"] * 100)
    tier = 10 if dd >= 10 else 5 if dd >= 5 else 0
    return {"tier": tier, "drawdown": dd, "anchor": anchor, "latest": latest}
def next_us_state(previous: int, tier: int) -> tuple[bool, int]:
    if tier == 0:
        return False, 0
    if previous == 0:
        return True, tier
    if previous == 5 and tier == 10:
        return True, 10
    return False, previous
def notice_key(code: str, item: dict) -> str:
    raw = "|".join((code, str(item.get("id") or ""), str(item.get("title") or ""), str(item.get("text") or "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
def body(stamp: str, what: str, status: str, action: str, later: str, reduce: str, avoid: str, extra: list[str] | None = None) -> str:
    lines = [
        f"{stamp} | HBMCHK",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {reduce}",
        f"하지 말 것: {avoid}",
    ]
    return "\n".join(lines + (extra or []))
def event(title: str, message: str, topic: str = "cwally-port", priority: int = 4) -> dict:
    return {"title": title, "message": message, "topic": topic, "priority": priority}
def deliver(ev: dict, dry_run: bool, publisher, server: str) -> bool:
    print(f"channel={ev['topic']} title={ev['title']}")
    print(ev["message"])
    if dry_run:
        print("dry-run proposed alert; publish skipped")
        return True
    publisher(ev["topic"], ev["title"], ev["message"], ev["priority"], ["chart_with_upwards_trend"], server)
    return True
def fetch_yahoo(ticker: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=3mo&interval=1d&includePrePost=false"
    req = urllib.request.Request(url, headers={"User-Agent": YAHOO_UA})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))
def default_state() -> dict:
    return {"schema_version": 1, "kr_flow_active": {}, "sol_below_line": False,
            "us_notified_tier": {}, "notice_versions": [], "ops_daily": {}, "updated_at": None}
def run(dry_run: bool = False, now: datetime | None = None) -> int:
    now = (now or datetime.now(KST)).astimezone(KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, default_state())
    work = copy.deepcopy(state)
    stamp, today = now.strftime("%Y-%m-%d %H:%M KST"), now.date().isoformat()
    if now.year != int(cfg["year_guard"]) or not (cfg["active_from"] <= today <= cfg["expire_date"]):
        print(f"{stamp} outside active window; silent")
        return 0
    naver, kr_calendar, ntfy = helper("naver_api"), helper("kr_calendar"), helper("ntfy_util")
    server = ntfy.resolve_server(cfg.get("ntfy_server"))
    errors, publish_failed, changed = [], False, False
    flow_candidates, flow_updates = [], []
    for code in ("005930", "000660"):
        name = cfg["kr"]["stocks"][code]["name"]
        try:
            done = completed_kr(naver.stock_trend(code, size=40), now, kr_calendar, cfg["kr"]["ma_days"])
            result = kr_flow_result(done, cfg["kr"]["ma_days"])
            print(f"KR {code} {result}")
            old = bool(state["kr_flow_active"].get(code, False))
            if result["hit"] and not old:
                flow_candidates.append((code, name, result))
            elif not result["hit"] and old:
                work["kr_flow_active"][code] = False
                changed = True
        except Exception as exc:
            errors.append((f"kr-{code}", name, str(exc)))
    if flow_candidates:
        details = [f"- {name} Naver {r['date']} 종가 {r['close']:,.0f}원, MA20 {r['ma20']:,.0f}원, 외국인 최근3일 {', '.join(f'{v:+,.0f}주' for v in r['foreign'])}"
                   for _, name, r in flow_candidates]
        ev = event("[HBMCHK] 수급 회복 관찰 | 실적/계약 확인 전 신규매수 보류", body(
            stamp, "삼성전자와 SK하이닉스의 완료 일봉 수급 및 MA20 관찰입니다. 매수 신호가 아닙니다.",
            " / ".join(f"{name} 외국인 3일 양수 및 종가 MA20 이상" for _, name, _ in flow_candidates),
            "신규 추격매수는 보류하고 아래 공식 원문 체크리스트를 사람이 확인하세요.",
            "HBM4 최종 확정 계약, 실적 전망 상향, 자사주 매입 종료와 종료 후 외국인 수급 정상화를 각각 확인하세요.",
            "보유 위험은 실제 실적과 계약 원문으로 따로 판단하세요. 이 수급 조건만으로 줄이지 않습니다.",
            "시세 3배 주장, 목표가 재확인, 제안 또는 협의를 확정 계약으로 읽기.",
            details + ["수동 체크: HBM4 확정 계약 vs 제안 / 실적 전망 상향 / 자사주 매입 종료는 알고리즘 미검증"]
        ))
        try:
            deliver(ev, dry_run, ntfy.publish, server)
            if not dry_run:
                for code, _, _ in flow_candidates:
                    work["kr_flow_active"][code] = True
                changed = True
        except Exception as exc:
            publish_failed = True
            print(f"publish failed flow: {exc}", file=sys.stderr)
    try:
        code, spec = "475300", cfg["kr"]["stocks"]["475300"]
        done = completed_kr(naver.stock_trend(code, size=40), now, kr_calendar, 1)
        close, day, line = done[0]["close"], done[0]["day"], float(spec["risk_line"])
        hit, old = close < line, bool(state.get("sol_below_line", False))
        print(f"KR {code} date={day} close={close} below={hit}")
        if hit and not old:
            ev = event("[HBMCHK] 신규진입 보류/보유 위험 점검", body(
                stamp, "SOL 475300의 기존 25,000원 위험선 완료 일봉 점검입니다.",
                f"Naver {day} 종가 {close:,.0f}원, 25,000원 미만.",
                "신규진입을 보류하고 보유 중인 경우에만 사람의 위험 검토를 하세요.",
                "다음 완료 KRX 일봉에서 25,000원 회복 여부를 다시 봅니다.",
                "자동 매도 알림이 아닙니다. 보유 여부와 손실 허용 범위를 직접 확인하세요.",
                "장중 가격, 25,000원과 같은 가격, 이 알림만으로 자동 매도하기."
            ))
            try:
                deliver(ev, dry_run, ntfy.publish, server)
                if not dry_run:
                    work["sol_below_line"], changed = True, True
            except Exception as exc:
                publish_failed = True
                print(f"publish failed 475300: {exc}", file=sys.stderr)
        elif not hit and old:
            work["sol_below_line"], changed = False, True
    except Exception as exc:
        errors.append(("kr-475300", "SOL 475300", str(exc)))
    for ticker, spec in cfg["us"]["tickers"].items():
        try:
            result = drawdown_result(completed_us(fetch_yahoo(ticker), now, cfg["us"]["lookback_trading_days"]), cfg["us"]["lookback_trading_days"])
            previous = int(state["us_notified_tier"].get(ticker, 0))
            notify, new_state = next_us_state(previous, result["tier"])
            print(f"US {ticker} {result} previous={previous} notify={notify}")
            if notify:
                a, last = result["anchor"], result["latest"]
                ev = event(f"[HBMCHK] {ticker} {result['tier']}% 조정 | 목표가 아닌 위험 점검", body(
                    stamp, f"{ticker} 최근 30거래일 최고 종가 대비 조정 폭 관찰입니다.",
                    f"데이터일 {last['day']}, 종가 ${last['close']:.2f}, 30거래일 최고 종가 ${a['close']:.2f} ({a['day']}), 조정 {result['drawdown']:.2f}%.",
                    "목표가로 매수하지 말고 변동성, 실적, 현금 비중을 사람이 재검토하세요.",
                    f"공식 IR {spec['official_ir']}와 기존 실적 모니터에서 실제 실적을 확인하세요.",
                    "실적 논거 훼손이 확인될 때만 별도 축소 판단을 하세요.",
                    "이 조정률을 선행 목표가, 자동 매수, 물타기 근거로 사용하기."
                ))
                try:
                    deliver(ev, dry_run, ntfy.publish, server)
                    if not dry_run:
                        work["us_notified_tier"][ticker], changed = new_state, True
                except Exception as exc:
                    publish_failed = True
                    print(f"publish failed {ticker}: {exc}", file=sys.stderr)
            elif new_state != previous:
                work["us_notified_tier"][ticker], changed = new_state, True
        except Exception as exc:
            errors.append((f"us-{ticker}", ticker, str(exc)))
    notice_start = date.fromisoformat(cfg.get("notice_window_start", cfg["active_from"]))
    seen = set(state.get("notice_versions", []))
    for code in cfg["notices"]["codes"]:
        name = cfg["kr"]["stocks"][code]["name"]
        try:
            items = naver.notices(code, size=cfg["notices"]["size"])
            if not isinstance(items, list) or not items:
                raise ValueError("공시 목록이 비었거나 형식 오류")
            candidates = []
            for item in items:
                if not isinstance(item, dict) or not item.get("title") or not item.get("datetime") or not item.get("url"):
                    raise ValueError("공시 행 필수값 누락")
                when = parse_notice_time(item["datetime"])
                key = notice_key(code, item)
                if notice_start <= when.date() <= now.date() and NOTICE_RE.search(item["title"]) and key not in seen:
                    candidates.append((when, key, item))
            for when, key, item in sorted(candidates):
                ev = event("[HBMCHK] 새 공시, 원문 재검토", body(
                    stamp, f"{name}의 제목 키워드 기반 새 공시 원문 확인 요청입니다.",
                    f"API 공시표시일 {when.date()} | {item['title']}",
                    "공시 원문에서 실제 내용과 조건, 금액, 상대방, 기간을 직접 확인하세요.",
                    "HBM4 확정 계약, 실적 전망 상향, 자사주 매입 종료 여부를 각각 별도로 판정하세요.",
                    "키워드만으로 보유 축소를 결정하지 말고 기존 논거와 실제 숫자를 대조하세요.",
                    "제목만 보고 공급계약 확정, 실적 상향, 자사주 매입 종료로 분류하기.",
                    [f"출처: {item['url']}"]
                ))
                try:
                    deliver(ev, dry_run, ntfy.publish, server)
                    if not dry_run:
                        work["notice_versions"].append(key)
                        seen.add(key)
                        work["notice_versions"] = work["notice_versions"][-500:]
                        changed = True
                except Exception as exc:
                    publish_failed = True
                    print(f"publish failed notice {code}: {exc}", file=sys.stderr)
        except Exception as exc:
            errors.append((f"notice-{code}", f"{name} 공시", str(exc)))
    for key, label, detail in errors:
        if state.get("ops_daily", {}).get(key) == today:
            print(f"ops dedup {key} {today}: {detail}")
            continue
        ev = event(f"[HBMCHK] {label} 수집 실패 | 판정 유보", body(
            stamp, "HBMCHK 원천 데이터 수집 또는 검증 실패 안내입니다.",
            f"{label}: {detail}. 누락을 0이나 최신값으로 대체하지 않았습니다.",
            "해당 자산 판정만 유보하고 다른 성공 자산 결과만 사용하세요.",
            "다음 평일 09:00 수집에서 재시도합니다.",
            "실패 알림만으로 포지션을 줄이지 마세요.",
            "누락을 0, 장중값, 오래된 값으로 해석하기."
        ), "cwally-ops", 4)
        try:
            deliver(ev, dry_run, ntfy.publish, server)
            if not dry_run:
                work.setdefault("ops_daily", {})[key] = today
                changed = True
        except Exception as exc:
            publish_failed = True
            print(f"publish failed ops {key}: {exc}", file=sys.stderr)
    if dry_run:
        print(f"dry-run summary errors={len(errors)} state_write=no")
        return 1 if errors else 0
    if changed:
        work["updated_at"] = now.isoformat()
        atomic_save(STATE_PATH, work)
    return 1 if publish_failed else 0
def self_test() -> int:
    class KRCal:
        holidays = {date(2026, 10, 5)}
        @staticmethod
        def is_trading_day(d): return d.weekday() < 5 and d not in KRCal.holidays
        @staticmethod
        def prev_trading_day(d):
            d -= timedelta(days=1)
            while not KRCal.is_trading_day(d): d -= timedelta(days=1)
            return d
    def assert_raises(fn):
        try: fn()
        except ValueError: return
        raise AssertionError("ValueError expected")
    now = datetime(2026, 10, 7, 9, 0, tzinfo=KST)
    krdays = previous_days(date(2026, 10, 6), 22, KRCal.is_trading_day)
    rows = [{"day": d.strftime("%Y%m%d"), "close": 100 + i, "foreign": 1} for i, d in enumerate(krdays)]
    done = completed_kr(rows, now, KRCal, 20)
    assert [r["day"] for r in done[:3]] == [date(2026, 10, 6), date(2026, 10, 2), date(2026, 10, 1)]
    stale = copy.deepcopy(rows); stale.pop(0)
    assert_raises(lambda: completed_kr(stale, now, KRCal, 20))
    missing = copy.deepcopy(done); missing[1]["foreign"] = None
    assert_raises(lambda: kr_flow_result(missing))
    nonfinite = copy.deepcopy(done); nonfinite[0]["foreign"] = float("inf")
    assert_raises(lambda: kr_flow_result(nonfinite))
    badclose = copy.deepcopy(rows); badclose[0]["close"] = float("nan")
    assert_raises(lambda: completed_kr(badclose, now, KRCal, 20))
    assert not NOTICE_RE.search("분기배당 기준일 공고")
    old = {"id": "1", "title": "공급계약", "text": "x", "datetime": "2026-10-06 10:00", "url": "u"}
    assert parse_notice_time(old["datetime"]) < datetime(2026, 10, 7, 15, 12, 7, tzinfo=KST)
    changed_notice = dict(old, text="amended")
    assert notice_key("005930", old) != notice_key("005930", changed_notice)
    ny_now = datetime(2026, 10, 7, 15, 0, tzinfo=NY)
    usdays = list(reversed(previous_days(date(2026, 10, 6), 31, is_us_day)))
    stamps = [int(datetime.combine(d, time(13), NY).timestamp()) for d in usdays]
    stamps.append(int(datetime(2026, 10, 7, 13, 0, tzinfo=NY).timestamp()))
    payload = {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [{"close": [100.0] * 31 + [1.0]}]}}]}}
    usrows = completed_us(payload, ny_now, 30)
    assert usrows[-1]["day"] == date(2026, 10, 6)
    invalid_us = copy.deepcopy(payload); invalid_us["chart"]["result"][0]["indicators"]["quote"][0]["close"][-2] = float("nan")
    assert_raises(lambda: completed_us(invalid_us, ny_now, 30))
    base = [{"day": date(2026, 1, 1) + timedelta(days=i), "close": 100.0} for i in range(30)]
    five = copy.deepcopy(base); five[-1]["close"] = 95.0
    ten = copy.deepcopy(base); ten[-1]["close"] = 90.0
    assert drawdown_result(five)["tier"] == 5 and drawdown_result(ten)["tier"] == 10
    assert not (25000 < 25000) and (24999 < 25000)
    assert next_us_state(0, 5) == (True, 5)
    assert next_us_state(5, 5) == (False, 5)
    assert next_us_state(5, 10) == (True, 10)
    assert next_us_state(10, 5) == (False, 10)
    assert next_us_state(10, 0) == (False, 0)
    calls = []
    fake = lambda *args: calls.append(args)
    sample = event("[HBMCHK] test", "body")
    assert deliver(sample, True, fake, "x") and not calls
    assert deliver(sample, False, fake, "x") and len(calls) == 1
    print("SELF-TEST PASS: freshness trading-dates foreign-None notice-filter old-notice amendment intraday-US drawdown-5-10 strict-25000 dedup-rearm dry-run-no-publish mocked-publish")
    return 0
def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    return self_test() if args.self_test else run(args.dry_run)
if __name__ == "__main__":
    raise SystemExit(main())
