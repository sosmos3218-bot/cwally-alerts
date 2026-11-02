#!/usr/bin/env python3
"""[FR-DE] 프랑스-독일 10년물 스프레드(OAT-Bund) 단계 경보 + 유럽 ETF 매수/매도 추천 (사용자 승인 2026-10-30).

단계 200/250/300/350bp: 아래에서 위로 넘을 때 단계마다 1번(p3/p4/p4/p5). 한 번에 여러 단계를 넘으면 가장 높은
단계 1건으로 보내고 넘은 단계는 모두 발송 처리. 단계선보다 20bp 아래로 내려가면 그 단계 재무장.
복귀: 2단계(250bp) 이상이 발송된 뒤 스프레드가 200bp 아래로 오면 p3 1번(에피소드마다 1번, 보내면 에피소드 종료).
데이터: CNBC FR10Y-FR / DE10Y-DE, 실패 시 네이버 시장지표 채권(FR10YT=RR, DE10YT=RR). 둘 다 실패면 판정 없음,
cwally-ops 하루 1번. ETF 종가는 네이버, 실패한 ETF 는 가격만 빼고 알림은 그대로.
flow-alerts.yml 평일 15:40 KST. modes: auto | dry(발송 없음, state 안 씀)
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "frde.json"
STATE_PATH = ROOT / "state" / "frde.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch_json(url: str) -> object:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def num(v) -> float | None:
    try:
        return float(str(v).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def sane(fr: float | None, de: float | None) -> bool:
    return fr is not None and de is not None and -2 < fr < 20 and -2 < de < 20


def from_cnbc(cfg: dict) -> dict:
    d = fetch_json(cfg["sources"]["primary"])
    qs = d["FormattedQuoteResult"]["FormattedQuote"]
    by = {q["symbol"]: q for q in qs}
    fr, de = by["FR10Y-FR"], by["DE10Y-DE"]
    frv, dev = num(fr.get("last")), num(de.get("last"))
    if not sane(frv, dev):
        raise RuntimeError(f"CNBC 값 이상 FR={fr.get('last')} DE={de.get('last')}")
    day = max(str(fr.get("last_time", ""))[:10], str(de.get("last_time", ""))[:10])
    return {"fr": frv, "de": dev, "date": day, "source": "CNBC"}


def from_naver(cfg: dict) -> dict:
    def rows(url):
        out = {}
        for r in (fetch_json(url).get("result") or []):
            v = num(r.get("closePrice"))
            if v is not None:
                out[str(r.get("localTradedAt", ""))[:10]] = v
        return out
    fr, de = rows(cfg["sources"]["fallback_fr"]), rows(cfg["sources"]["fallback_de"])
    common = sorted(set(fr) & set(de))
    if not common:
        raise RuntimeError("네이버 FR/DE 같은 날짜 없음")
    d = common[-1]
    if not sane(fr[d], de[d]):
        raise RuntimeError("네이버 값 이상")
    return {"fr": fr[d], "de": de[d], "date": d, "source": "네이버 시장지표"}


def get_yields(cfg: dict) -> tuple[dict | None, list[str]]:
    errs = []
    for fn in (from_cnbc, from_naver):
        try:
            y = fn(cfg)
            y["spread"] = round((y["fr"] - y["de"]) * 100, 1)
            return y, errs
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{fn.__name__[5:]}: {type(exc).__name__} {str(exc)[:100]}")
    return None, errs


def etf_close(code: str) -> tuple[str, float] | None:
    try:
        raw = fetch_json(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=2&page=1")
        r = sorted(raw, key=lambda x: x.get("localTradedAt", ""))[-1]
        v = num(r.get("closePrice"))
        return (str(r.get("localTradedAt", ""))[:10], v) if v else None
    except Exception:  # noqa: BLE001
        return None


# ---------- 판정 ----------
def judge(cfg: dict, st: dict, spread: float) -> tuple[dict | None, list[str], bool]:
    """(가장 높은 새 단계 or None, 새로 넘은 단계 key 목록, 복귀 여부). st 갱신."""
    arm = st.setdefault("armed", {s["key"]: True for s in cfg["stages"]})
    rb = cfg["rearm_bp"]
    crossed, top = [], None
    for s in cfg["stages"]:
        a = arm.setdefault(s["key"], True)
        if spread >= s["level"] and a:
            arm[s["key"]] = False
            crossed.append(s["key"])
            top = s
        elif not a and spread < s["level"] - rb:
            arm[s["key"]] = True
    if top:
        idx = [s["key"] for s in cfg["stages"]].index(top["key"]) + 1
        st["episode_max"] = max(int(st.get("episode_max") or 0), idx)
    relief = False
    if not top and spread < cfg["relief_level"] and int(st.get("episode_max") or 0) >= cfg["relief_min_stage"]:
        relief = True
        st["episode_max"] = 0
    elif not top and spread < cfg["relief_level"] and int(st.get("episode_max") or 0) > 0:
        st["episode_max"] = 0 if spread < cfg["relief_level"] - rb else st["episode_max"]
    return top, crossed, relief


def rec_lines(cfg: dict, recs: list, prices: dict) -> list[str]:
    out = []
    for code, text in recs:
        if code == "*":
            out.append(f"- 전체: {text}")
            continue
        p = prices.get(code)
        ptxt = f" ({p[0][5:].replace('-', '/')} 종가 {p[1]:,.0f}원)" if p else ""
        out.append(f"- {code} {cfg['etfs'][code]}{ptxt}: {text}")
    return out


def build(cfg: dict, y: dict, stamp: str, stage: dict | None, crossed: list[str], relief: bool, prices: dict, stage_no: int) -> tuple[str, str, int]:
    if relief:
        title, pri, recs = cfg["relief"]["title"], cfg["relief"]["priority"], cfg["relief"]["recs"]
        stage_txt = "복귀(2단계 이상 발송 뒤 200bp 아래)"
    else:
        title, pri, recs = stage["title"], stage["priority"], stage["recs"]
        names = [f"{s['level']}bp" for s in cfg["stages"] if s["key"] in crossed]
        stage_txt = f"{stage_no}단계 {stage['level']}bp" + (f" (이번에 함께 넘은 선: {', '.join(names)})" if len(names) > 1 else "")
    lines = [
        f"{stamp} | FR-DE | 프랑스-독일 10년물 스프레드",
        "이게 뭐 알림: 프랑스 OAT 10년물과 독일 Bund 10년물 금리차 단계 경보와 유럽 ETF 매수/매도 추천. 매매 지시 아님.",
        f"지금 결과/상태: 스프레드 {y['spread']:.1f}bp, 프랑스 10년 {y['fr']:.3f}%, 독일 10년 {y['de']:.3f}% ({y['date']} 기준, {y['source']}). 단계: {stage_txt}.",
        "지금 할 일 (ETF 추천):",
        *rec_lines(cfg, recs, prices),
        f"나중에: 단계선보다 {cfg['rearm_bp']}bp 아래로 내려간 뒤 다시 넘으면 다시 알림. 2단계 이상이 울린 뒤 200bp 아래로 오면 복귀 알림 1번.",
        "익절/줄일 때: 위 추천 순서대로 레버리지, 환노출부터 줄이기.",
        "하지 말 것: 한 번에 전량 매매, 장중 한 번의 급등만 보고 추격 매수.",
        cfg["closing_line"],
    ]
    msg = "\n".join(lines)
    assert "\u00b7" not in title + msg and "\u2014" not in title + msg
    assert "확인 불가" not in title and "조회 실패" not in title
    return title, msg, pri


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(mode: str, now: datetime, cfg: dict, pub=publish, yields_fn=None, price_fn=None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.date().isoformat()
    st = load_state()
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    y, errs = (yields_fn or get_yields)(cfg)
    out = []
    if y is None:
        print(f"yields unavailable: {errs}")
        if st.get("fail_date") != today:
            title = "[FR-DE] VGK 금리 조회 실패 | 스프레드 판정 보류"
            msg = "\n".join([f"{stamp} | FR-DE | 데이터 읽기 실패",
                             "이게 뭐 알림: 프랑스-독일 10년물 스프레드 경보의 데이터 읽기 실패 안내. 매매 신호 아님.",
                             f"지금 결과/상태: {' / '.join(errs)}. 오늘은 판정하지 않았습니다.",
                             "지금 할 일: 필요하면 증권 앱에서 프랑스, 독일 10년물 금리를 직접 보세요.",
                             "나중에: 다음 평일 15:40 KST 에 다시 읽습니다.",
                             "익절/줄일 때: 이 안내로 비중을 바꾸지 마세요.",
                             "하지 말 것: 읽기 실패를 스프레드 안정으로 읽기."])
            out.append((title, msg, 3))
            st["fail_date"] = today
    else:
        print(f"yields: {y}")
        top, crossed, relief = judge(cfg, st, y["spread"])
        st["last"] = {**y, "at": stamp}
        if top or relief:
            pf = price_fn or etf_close
            prices = {c: p for c in cfg["etfs"] if (p := pf(c))}
            no = [s["key"] for s in cfg["stages"]].index(top["key"]) + 1 if top else 0
            out.append(build(cfg, y, stamp, top, crossed, relief, prices, no))
            st.setdefault("history", []).append({"at": stamp, "spread": y["spread"], "event": top["key"] if top else "relief"})
            st["history"] = st["history"][-30:]
    for title, msg, pri in out:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
    if not out:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for title, msg, pri in out:
        pub(topic, title, msg, pri, ["chart_with_upwards_trend"], server, config_name="frde")
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry"}:
        print("usage: frde_monitor.py <auto|dry>")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    return run(mode, datetime.now(KST), cfg)


if __name__ == "__main__":
    sys.exit(main())
