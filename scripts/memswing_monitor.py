#!/usr/bin/env python3
"""[MEMSWING] 삼성전자, SK하이닉스 스윙 진입 조건 1회 판정 (2026-10-19 확정 수급).

조건(종목별, 모두 충족): 외국인 순매수 2거래일 연속(10/16, 10/19) AND KRX 정규장 종가 > 기준가.
one-shot: 2026-10-19 18:30 / 19:30 KST (oneshot_pick). 18:30에 10/19 수급 행이 없으면 침묵,
19:30(또는 그 뒤 늦은 백업)에는 결과 또는 미게시 안내를 보낸다. 오늘 1회만 발송.
mode: auto (기본) | force (날짜/발송 기록 무시, 판정 후 발송) | dry (발송 없음, state 쓰기 없음)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "memswing.json"
STATE_PATH = ROOT / "state" / "memswing.json"
UA = "Mozilla/5.0 (cwally-alerts memswing; +mailto:sosmos3218@gmail.com)"


def get_json(url: str, referer: str | None = None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    last = None
    for i in range(3):  # 일시적 SSL/네트워크 오류 재시도
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception as exc:
            last = exc
            time.sleep(3 * (i + 1))
    raise last


def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", "").replace("%", ""))
    except ValueError:
        return None


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sent": False, "updated_at": None, "last_title": None}


def fetch_stock(code: str, cfg: dict, day: str) -> dict:
    """Naver 일별 수급(주 출처) + Daum 수급/KRX 종가(교차확인). day = YYYYMMDD."""
    src = cfg["sources"]
    out: dict = {"code": code, "errors": []}
    rows = get_json(src["trend"].format(code=code))  # 실패하면 예외 = 조회 실패
    rows = [r for r in rows if isinstance(r, dict) and r.get("bizdate")]
    rows.sort(key=lambda r: r["bizdate"], reverse=True)
    today = next((r for r in rows if r["bizdate"] == day), None)
    out["posted"] = today is not None
    if today is None:
        return out
    prev = next((r for r in rows if r["bizdate"] < day), None)
    out["prev_day"] = prev["bizdate"] if prev else None
    out["f_today"] = num(today.get("foreignerPureBuyQuant"))
    out["o_today"] = num(today.get("organPureBuyQuant"))
    out["i_today"] = num(today.get("individualPureBuyQuant"))
    out["f_prev"] = num(prev.get("foreignerPureBuyQuant")) if prev else None
    out["naver_close"] = num(today.get("closePrice"))  # 네이버 일별 값(NXT 포함 통합일 수 있음)
    # KRX 정규장 종가: Daum 일별 시세 (KRX 기준), Naver polling closePrice로 교차확인
    ref = "https://finance.daum.net/"
    try:
        q = get_json(src["krx_close"].format(code=code), ref).get("data") or []
        qd = next((r for r in q if str(r.get("date", "")).replace("-", "")[:8] == day), None)
        if qd:
            out["krx_close"] = num(qd.get("tradePrice"))
            out["krx_prev_close"] = num(qd.get("prevClosingPrice"))
    except Exception as exc:
        out["errors"].append(f"daum quote: {exc}")
    try:
        p = (get_json(src["krx_close_check"].format(code=code)).get("datas") or [{}])[0]
        if str(p.get("localTradedAt", ""))[:10].replace("-", "") == day:
            out["poll_close"] = num(p.get("closePriceRaw") or p.get("closePrice"))
            out["poll_rate"] = num(p.get("fluctuationsRatioRaw") or p.get("fluctuationsRatio"))
    except Exception as exc:
        out["errors"].append(f"naver polling: {exc}")
    if out.get("krx_close") is None and out.get("poll_close") is not None:
        out["krx_close"] = out["poll_close"]
    # Daum 수급 교차확인 (부호만 비교)
    try:
        d = get_json(src["investor_check"].format(code=code), ref).get("data") or []
        dd = {str(r.get("date", "")).replace("-", "")[:8]: num(r.get("foreignStraightPurchaseVolume")) for r in d}
        out["daum_f_today"] = dd.get(day)
        out["daum_f_prev"] = dd.get(out["prev_day"]) if out.get("prev_day") else None
    except Exception as exc:
        out["errors"].append(f"daum investor: {exc}")
    return out


def sign_ok(naver: float | None, daum: float | None) -> bool:
    """Daum 값이 있으면 Naver와 부호가 같아야 한다. 없으면 Naver만으로 판단."""
    if naver is None:
        return False
    if daum is None:
        return True
    return (naver > 0) == (daum > 0)


def fmt_q(v: float | None) -> str:
    return "확인 불가" if v is None else f"{v:+,.0f}주"


def judge(s: dict, rule: dict) -> dict:
    thr, stop, tgt = rule["threshold"], rule["stop"], rule["target"]
    close = s.get("krx_close")
    c_prev = s.get("f_prev") is not None and s["f_prev"] > 0 and sign_ok(s.get("f_prev"), s.get("daum_f_prev"))
    c_today = s.get("f_today") is not None and s["f_today"] > 0 and sign_ok(s.get("f_today"), s.get("daum_f_today"))
    c_close = close is not None and close > thr
    mism = [k for k, a, b in (("10/16", s.get("f_prev"), s.get("daum_f_prev")), ("10/19", s.get("f_today"), s.get("daum_f_today")))
            if a is not None and b is not None and (a > 0) != (b > 0)]
    res = {"met": c_prev and c_today and c_close and s.get("prev_day") == "20261016", "c_prev": c_prev, "c_today": c_today,
           "c_close": c_close, "close": close, "mismatch": mism}
    if close:
        res["stop_pct"] = (stop / close - 1) * 100
        res["tgt_pct"] = (tgt / close - 1) * 100
        risk, reward = close - stop, tgt - close
        res["rr"] = reward / risk if risk > 0 else None
    return res


def ox(b: bool) -> str:
    return "O" if b else "X"


def build(cfg: dict, data: dict, stamp: str) -> tuple[str, str, int]:
    lines_state, met_names, miss_names = [], [], []
    stop_lines, tp_lines = [], []
    for code, rule in cfg["stocks"].items():
        s, name = data[code], rule["display"]
        j = judge(s, rule)
        (met_names if j["met"] else miss_names).append(rule["short"])
        close = j["close"]
        rate = s.get("poll_rate")
        if rate is None and close and s.get("krx_prev_close"):
            rate = (close / s["krx_prev_close"] - 1) * 100
        ctext = "확인 불가" if close is None else f"{close:,.0f}원" + (f"({rate:+.2f}%)" if rate is not None else "")
        lines_state.append(
            f"- {name}: KRX 종가 {ctext}, 기준 {rule['threshold']:,}원 초과 {ox(j['c_close'])} / "
            f"외인 10/16 {fmt_q(s.get('f_prev'))} {ox(j['c_prev'])}, 10/19 {fmt_q(s.get('f_today'))} {ox(j['c_today'])} / "
            f"기관 {fmt_q(s.get('o_today'))}, 개인 {fmt_q(s.get('i_today'))} => {'충족' if j['met'] else '미충족'}"
        )
        if j["mismatch"]:
            lines_state.append(f"- {name}: 네이버와 다음 외인 부호 불일치({', '.join(j['mismatch'])}) => 해당 일은 X 처리")
        if s.get("prev_day") != "20261016":
            lines_state.append(f"- {name}: 직전 거래일이 10/16이 아님({s.get('prev_day')}) => 미충족 처리")
        if close:
            stop_lines.append(f"{rule['short']} 종가 {rule['stop']:,}원 미만(현재 대비 {j['stop_pct']:.1f}%)")
            rr = f", 손익비 약 {j['rr']:.1f}" if j.get("rr") else ""
            tp_lines.append(f"{rule['short']} 1차 {rule['target']:,}원(+{j['tgt_pct']:.1f}%{rr}, {rule['target_note']})")
        else:
            stop_lines.append(f"{rule['short']} 종가 {rule['stop']:,}원 미만")
            tp_lines.append(f"{rule['short']} 1차 {rule['target']:,}원")
    if met_names and not miss_names:
        title = f"[MEMSWING] {', '.join(met_names)} 진입 조건 충족 | 내일 분할 진입 검토"
        todo = "지금 할 일: 내일(10/20) 장중 손절선 위 유지를 확인하고 1/2만 분할 진입 검토. 갭 상승 3% 이상이면 추격 말고 눌림 대기."
        prio = 4
    elif met_names:
        title = f"[MEMSWING] {', '.join(met_names)}만 진입 조건 충족 | {', '.join(miss_names)} 보류"
        todo = f"지금 할 일: {', '.join(met_names)}만 내일(10/20) 손절선 위 유지 확인 후 1/2 분할 진입 검토. {', '.join(miss_names)}는 신규 진입 보류."
        prio = 4
    else:
        title = "[MEMSWING] 진입 조건 미충족 | 신규 진입 보류"
        todo = "지금 할 일: 신규 진입 보류. 보유분이 있으면 아래 손절선만 지키세요."
        prio = 3
    body = [
        f"{stamp} | 삼성전자, SK하이닉스 스윙 진입 점검",
        "이게 뭐 알림: 10/19 KRX 확정 수급으로 스윙(3~10거래일) 진입 조건을 한 번 점검한 결과. 조건: 외인 순매수 2일 연속(10/16, 10/19) 및 KRX 종가 기준가 초과.",
        "지금 결과/상태:",
        *lines_state,
        f"데이터: 네이버 일별 수급(순매수 주식 수, 18:00 이후 게시값 = 확정 기준), 다음 수급 부호 교차확인. 종가는 KRX 정규장(다음/네이버). 조회 {stamp}.",
        todo + " 손절: " + ", ".join(stop_lines) + f". 보유는 최대 {cfg['hold_until'][5:].replace('-', '/')}(10거래일)까지.",
        "나중에: 조건 미충족 종목은 외인 순매수 2일 연속과 기준가 위 종가가 다시 나오면 재점검. 이 알림은 오늘 1회로 끝.",
        "익절/줄일 때: " + ", ".join(tp_lines) + "에서 절반 익절, 나머지는 5일선 종가 이탈 시 정리.",
        "하지 말 것: 장후 NXT(넥스트레이드)나 시간외 가격으로 판정 바꾸기, 추격, 신용, 미수. 투자 권유 아님.",
    ]
    return title, "\n".join(body), prio


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_state()
    now = datetime.now(KST)
    if dry and os.environ.get("MEMSWING_NOW"):  # dry 검증 전용 시각 덮어쓰기
        now = datetime.strptime(os.environ["MEMSWING_NOW"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    fire_day = cfg["fire_day"]
    day = fire_day.replace("-", "")
    if now.year != int(cfg.get("year_guard") or 2026):
        print(f"year {now.year}; silent")
        return 0
    if mode == "auto":
        if now.date().isoformat() != fire_day:
            print(f"not {fire_day}; silent")
            return 0
        if now.strftime("%Y-%m-%d %H:%M") > cfg["ends_kst"]:
            print("past ends_kst; silent")
            return 0
        if state.get("sent"):
            print("already sent today; silent")
            return 0
        if now.strftime("%H:%M") < cfg["final_after_kst"]:
            print("before final investor data time; silent")
            return 0
    last_try = now.strftime("%H:%M") >= cfg["last_try_kst"] or mode == "force"

    data, err = {}, None
    try:
        for code in cfg["stocks"]:
            data[code] = fetch_stock(code, cfg, day)
            print(json.dumps(data[code], ensure_ascii=False))
    except Exception as exc:
        err = str(exc)
        print(f"fetch error: {err}")
    posted = err is None and all(d.get("posted") for d in data.values())
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))

    if not posted:
        if not last_try:
            print("10/19 investor row not posted yet (or fetch error); wait for 19:30 one-shot; silent, no state write")
            return 0
        what = "조회 실패" if err else "미게시"
        title = f"[MEMSWING] 10/19 확정 수급 {what} | 진입 판단 보류"
        msg = "\n".join([
            f"{stamp} | 삼성전자, SK하이닉스 스윙 진입 점검",
            "이게 뭐 알림: 10/19 확정 수급으로 스윙 진입 조건을 점검하려 했으나 데이터를 확정할 수 없었음.",
            f"지금 결과/상태: 외인 순매수 2일 연속 여부 확인 불가 ({'오류: ' + err[:120] if err else '네이버 일별 수급에 10/19 행 없음'}).",
            "지금 할 일: 신규 진입 보류. 내일 아침 10/19 확정 수급을 직접 확인.",
            "나중에: 확정 수급이 나오면 같은 조건(외인 2일 연속 순매수, 기준가 위 종가)으로 다시 판단.",
            "익절/줄일 때: 보유분은 종가 기준 손절선(삼성 204,500원, 하이닉스 1,238,000원) 미만이면 정리.",
            "하지 말 것: 조회 실패를 매수 또는 매도 신호로 읽기. 투자 권유 아님.",
        ])
        prio = 3
    else:
        title, msg, prio = build(cfg, data, stamp)

    print(title)
    print(msg)
    print(f"topic={route_topic(topic, title)} send={not dry}")
    if dry:
        print("DRY RUN: no ntfy, no state write")
        return 0
    publish(topic, title, msg, prio, ["chart_with_upwards_trend"], server)
    state.update({"sent": True, "last_title": title, "updated_at": now.isoformat()})
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
