#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "portlight.json"
STATE_PATH = ROOT / "state" / "portlight.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback.update(data)
        return fallback
    except json.JSONDecodeError:
        return dict(fallback)


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def yahoo_closes(ticker: str) -> list[float]:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=1mo"
    )
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    quotes = result["indicators"]["quote"][0]["close"]
    closes = [float(x) for x in quotes if x is not None]
    if not closes:
        raise RuntimeError(f"no closes {ticker}")
    return closes


def pct(closes: list[float], sessions: int) -> float | None:
    if len(closes) < sessions + 1:
        return None
    old, new = closes[-(sessions + 1)], closes[-1]
    if old == 0:
        return None
    return (new / old - 1.0) * 100.0


def ecos_series(cfg: dict, item: str) -> list[float]:
    ecos = cfg.get("ecos") or {}
    key = os.environ.get(ecos.get("key_env") or "ECOS_API_KEY") or ecos.get("sample_key") or "SAMPLE"
    stat = ecos.get("stat_code") or "817Y002"
    end = datetime.now(KST)
    start = end - timedelta(days=21)
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{urllib.parse.quote(key)}"
        f"/json/kr/1/40/{stat}/D/{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}/{item}"
    )
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        rows = (payload.get("StatisticSearch") or {}).get("row") or []
        vals = []
        for row in rows:
            raw = row.get("DATA_VALUE")
            if raw in (None, ""):
                continue
            vals.append((str(row.get("TIME") or ""), float(raw)))
        vals.sort()
        return [v for _, v in vals]
    except Exception as exc:
        print(f"ecos fail {item}: {exc}", file=sys.stderr)
        return []


def naver_yield(code: str):
    """Latest KTB yield via Naver mobile marketIndex API (legacy interestDailyQuote is 410)."""
    if not code:
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import naver_api

    try:
        val, _day = naver_api.bond_close(code)
        return val
    except Exception as exc:
        print(f"naver fail {code}: {exc}", file=sys.stderr)
        return None


def last_and_5d(series: list[float]) -> tuple[float | None, float | None]:
    if not series:
        return None, None
    last = series[-1]
    if len(series) < 6:
        return last, None
    return last, (last - series[-6]) * 100.0


def fmt(v: float | None, nd: int = 2, suffix: str = "") -> str:
    if v is None:
        return "조회실패"
    return f"{v:.{nd}f}{suffix}"


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "조회실패"
    return f"{v:+.2f}%"


def score_rate(k10: float | None, bp5: float | None, hike: bool, rules: dict) -> str:
    if k10 is not None and k10 >= float(rules["rate_red_level"]):
        return "홍"
    if bp5 is not None and bp5 >= float(rules["rate_red_5d_bp"]):
        return "홍"
    if k10 is not None and k10 >= float(rules["rate_yellow_level"]):
        return "황"
    if bp5 is not None and bp5 >= float(rules["rate_yellow_5d_bp"]):
        return "황"
    if hike:
        return "황"
    return "녹"


def score_chip(chip_1d: float | None, chip_5d: float | None, kospi_1d: float | None, kospi_5d: float | None, rules: dict) -> str:
    if chip_1d is not None and chip_1d <= float(rules["chip_red_1d"]):
        return "홍"
    if chip_5d is not None and chip_5d <= float(rules["chip_red_5d"]):
        return "홍"
    if kospi_1d is not None and kospi_1d <= float(rules["kospi_red_1d"]):
        return "홍"
    if chip_5d is not None and chip_5d >= float(rules["chip_yellow_5d"]):
        return "황"
    if chip_5d is not None and kospi_5d is not None and chip_5d >= 3.0 and kospi_5d <= 0.5:
        return "황"
    return "녹"


def stance(rate: str, chip: str) -> str:
    reds = [rate, chip].count("홍")
    yellows = [rate, chip].count("황")
    if reds >= 2 or (reds == 1 and yellows >= 1):
        return "보수"
    if reds == 1 or yellows >= 2:
        return "중립-보수"
    if yellows == 1:
        return "중립-보수"
    return "중립"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    week = now.strftime("%G-W%V")
    state = load_json(STATE_PATH, {"weekly_sent_week": None})
    rules = cfg.get("rules") or {}
    hike = bool(cfg.get("hike_option"))
    tickers = cfg.get("tickers") or {}

    fetch_fail = []
    series: dict[str, list[float]] = {}
    for key in ("kospi", "samsung", "hynix", "us10y"):
        try:
            series[key] = yahoo_closes(tickers[key])
        except Exception as exc:
            print(f"yahoo fail {key}: {exc}", file=sys.stderr)
            series[key] = []
            fetch_fail.append(key)

    k3s = ecos_series(cfg, (cfg.get("ecos") or {}).get("ktb3_item") or "010200000")
    k10s = ecos_series(cfg, (cfg.get("ecos") or {}).get("ktb10_item") or "010210000")
    naver = cfg.get("naver_yield") or {}
    if not k3s:
        nv = naver_yield(naver.get("ktb3") or "IRR_GOVT03Y")
        if nv is not None:
            k3s = [nv]
    if not k10s:
        nv = naver_yield(naver.get("ktb10") or "IRR_GOVT10Y")
        if nv is not None:
            k10s = [nv]
    if not k3s:
        fetch_fail.append("ktb3")
    if not k10s:
        fetch_fail.append("ktb10")

    k3, _ = last_and_5d(k3s)
    k10, k10_bp = last_and_5d(k10s)
    us10_closes = series.get("us10y") or []
    us10 = us10_closes[-1] if us10_closes else None
    us10_bp = None
    if len(us10_closes) >= 6:
        us10_bp = (us10_closes[-1] - us10_closes[-6]) * 100.0

    kospi = series.get("kospi") or []
    sam = series.get("samsung") or []
    hyn = series.get("hynix") or []
    kospi_1d, kospi_5d = pct(kospi, 1) if kospi else None, pct(kospi, 5) if kospi else None
    sam_1d, sam_5d = pct(sam, 1) if sam else None, pct(sam, 5) if sam else None
    hyn_1d, hyn_5d = pct(hyn, 1) if hyn else None, pct(hyn, 5) if hyn else None
    chip_1d = None if None in (sam_1d, hyn_1d) else (sam_1d + hyn_1d) / 2.0
    chip_5d = None if None in (sam_5d, hyn_5d) else (sam_5d + hyn_5d) / 2.0

    rate = score_rate(k10, k10_bp, hike, rules)
    chip = score_chip(chip_1d, chip_5d, kospi_1d, kospi_5d, rules)
    pos = stance(rate, chip)
    bok = cfg.get("bok_base_pct")

    sunday = now.weekday() == 6
    should = mode in {"weekly", "force"} or (mode == "auto" and sunday)
    if mode == "event":
        print("portlight event; weekly only")
        state["updated_at"] = now.isoformat()
        save_state(state)
        return 0
    if not should:
        print("portlight silent; wait sunday 21:00 KST")
        state["updated_at"] = now.isoformat()
        state["last_skip_reason"] = "not_sunday"
        save_state(state)
        return 0
    if mode != "force" and state.get("weekly_sent_week") == week:
        print("portlight weekly already sent")
        state["updated_at"] = now.isoformat()
        state["last_skip_reason"] = "dup_week"
        save_state(state)
        return 0

    if {"kospi", "samsung", "hynix"} <= set(fetch_fail) or "ktb10" in fetch_fail:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            "[PORTLIGHT] 조회 실패 | 신호등 미적용",
            "\n".join(
                [
                    f"{now.strftime('%Y-%m-%d %H:%M KST')} | PORTLIGHT",
                    "이게 뭐 알림: 김효진 4신호등 주간 채점.",
                    f"지금 결과: 조회 실패. 항목={','.join(fetch_fail) or '없음'}.",
                    "지금 할 일: 오늘 포지션 변경 금지. 다음 주 일요일 재시도.",
                    "나중에: 시세, ECOS 확인 후 force 재실행.",
                    "익절/줄일 때: 적용 없음.",
                    "하지 말 것: 조회 실패를 홍으로 간주하지 말 것.",
                ]
            ),
            4,
            ["warning", "chart_with_downwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        state.update({"updated_at": now.isoformat(), "weekly_sent_week": week, "last_skip_reason": "fetch_fail_alert"})
        save_state(state)
        return 1

    title = f"[PORTLIGHT] {pos} | 금리 {rate} 반도체 {chip}"
    body = "\n".join(
        [
            f"{now.strftime('%Y-%m-%d %H:%M KST')} | PORTLIGHT 주간",
            "이게 뭐 알림: 김효진 4신호등 주간 채점. 투자권유 아님.",
            (
                f"지금 결과: 금리 {rate}, 반도체 {chip}, 종합 {pos}. "
                f"한은 {fmt(bok, 2, '%')}, 국고3년 {fmt(k3)}%, 국고10년 {fmt(k10)}%(5일 {fmt(k10_bp, 1, 'bp')}), "
                f"미10년 {fmt(us10)}%(5일 {fmt(us10_bp, 1, 'bp')}). "
                f"코스피 {fmt(kospi[-1] if kospi else None, 2)} 일 {fmt_pct(kospi_1d)} / 5일 {fmt_pct(kospi_5d)}. "
                f"삼전 {fmt(sam[-1] if sam else None, 0)} 일 {fmt_pct(sam_1d)} / 5일 {fmt_pct(sam_5d)}. "
                f"닉스 {fmt(hyn[-1] if hyn else None, 0)} 일 {fmt_pct(hyn_1d)} / 5일 {fmt_pct(hyn_5d)}."
            ),
            f"지금 할 일: 다음 주 포지션 {pos}. 신규는 레버리지 없이.",
            "나중에: 국고10년 4.50% 또는 5일 +10bp면 금리 경고.",
            "익절/줄일 때: 칩 평균 1일 -3.5% 또는 5일 -7% 또는 코스피 1일 -2.5%면 반도체 홍.",
            "하지 말 것: 신용으로 반도체 추격매수. 금리 민감 고밸류 신규 확대.",
        ]
    )
    pri = 4 if pos == "보수" else 3
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        body,
        pri,
        ["traffic_light", "chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )
    state.update(
        {
            "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
            "weekly_sent_week": week,
            "last_skip_reason": None,
            "last_stance": pos,
            "last_rate": rate,
            "last_chip": chip,
            "last_ktb10": k10,
            "last_ktb10_bp5": k10_bp,
        }
    )
    save_state(state)
    print(title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
