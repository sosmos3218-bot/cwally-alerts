#!/usr/bin/env python3
"""삼성전자우(005935) + SK하이닉스(000660) 장마감 수급 A/B/C 재판정.

A 유지: 삼성전자우 외인+기관 동반 순매도 AND 하이닉스 외인 순매도
B 전환: 삼성전자우 외인+기관 동반 순매수 AND 하이닉스 외인 매도 중단/순매수
C 유보: 그 외
state.base 가 비어 있으면 첫 실행일 수치를 base 로 저장(방식 2).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "configs" / "secu_flow.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "state" / "secu_flow.json"
API = "https://m.stock.naver.com/api/stock/{code}/trend?pageSize=5"
LABEL = {
    "A": "A 유지 | 추격매수 보류",
    "B": "B 전환 | 재진입 검토",
    "C": "C 유보 | 하루 더 관찰",
}


def to_int(s: str) -> int:
    return int(str(s).replace(",", "").replace("+", "").strip() or 0)


def regular_close(code: str, day: str) -> float:
    """KRX regular-session close for day (YYYYMMDD) via krx_close (Daum, else Naver 15:30 bar).

    Naver trend closePrice mixes NXT and after-hours prints (2026-11-08). Quantities stay Naver.
    """
    import krx_close

    for b in krx_close.regular_bars(code, 5):
        if krx_close.ymd(b.day) == day:
            return b.close
    raise RuntimeError(f"{code} {day} KRX 정규장 종가 없음")


def fetch(code: str, day: str) -> dict | None:
    import requests

    r = requests.get(API.format(code=code), headers={"User-Agent": "Mozilla/5.0 cwally-alerts"}, timeout=20)
    r.raise_for_status()
    for row in r.json():
        if row.get("bizdate") == day:
            close = regular_close(code, day)
            fq, oq = to_int(row["foreignerPureBuyQuant"]), to_int(row["organPureBuyQuant"])
            return {"close": close, "frgn_q": fq, "inst_q": oq, "frgn_won": fq * close, "inst_won": oq * close}
    return None  # 오늘 행 없음 = 휴장 또는 미집계


def judge(sf: int, si: int, hf: int) -> str:
    if sf < 0 and si < 0 and hf < 0:
        return "A"
    if sf > 0 and si > 0 and hf >= 0:
        return "B"
    return "C"


def eok(won: float) -> str:
    if abs(won) >= 1e12:
        return f"{won / 1e12:+.2f}조"
    return f"{won / 1e8:+,.0f}억"


def load_state() -> dict:
    st = {"base": None, "base_date": None, "last_day": None, "last_verdict": None, "history": []}
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def main() -> int:
    now = datetime.now(KST)
    sys.path.insert(0, str(ROOT / "scripts"))
    import kr_calendar

    session = kr_calendar.session_day(now)
    if not kr_calendar.is_trading_day(session):
        print(f"{session} KRX 휴장/주말 skip")
        return 0
    if session == now.date() and now.strftime("%H:%M") < "15:35":
        print("장중 수급(잠정치)이라 판정 안 함; silent")
        return 0
    day = session.strftime("%Y%m%d")
    st = load_state()
    if st.get("last_day") == day:
        print("already sent today; silent")
        return 0

    try:
        sec = fetch("005935", day)
        hyx = fetch("000660", day)
    except Exception as exc:  # 조회 실패 = 조건 미충족 아님
        publish(resolve_topic("ops"), "[FLOW] 005935/000660 조회 실패 | 수급 판정 미갱신",
                f"삼성전자우·하이닉스 수급 조회 실패: {exc}\n판정은 보내지 않았습니다. 다음 영업일 자동 재시도.", 3, ["warning"])
        return 0
    if sec is None and hyx is None:
        # 영업일인데 행이 없으면 휴장이 아니라 미게시(지연)다. 판정하지 않고 다음 슬롯 재시도.
        print(f"{day} 영업일 데이터 미게시; silent retry next slot")
        return 0
    if sec is None or hyx is None:
        publish(resolve_topic("ops"), "[FLOW] 005935/000660 수급 누락 | 판정 미갱신",
                f"{day} 삼성전자우={'OK' if sec else '누락'}, 하이닉스={'OK' if hyx else '누락'}. 판정은 보내지 않았습니다.", 3, ["warning"])
        return 0

    today = {"day": day, "sec_frgn": sec["frgn_won"], "sec_inst": sec["inst_won"], "hyx_frgn": hyx["frgn_won"]}
    if st.get("base") is None:
        st["base"], st["base_date"] = today, day

    v = judge(sec["frgn_q"], sec["inst_q"], hyx["frgn_q"])
    prev = st.get("last_verdict")
    base = st["base"]
    move = f"직전 {prev} → 오늘 {v}" if prev else f"첫 판정 {v}"

    do_now = {
        "A": "새로 추격해 사지 마세요. 보유분은 기존 손절선만 지키세요.",
        "B": "재진입 조건 후보입니다. 가격이 계획선 안인지 먼저 확인하세요.",
        "C": "외인·기관 방향이 갈렸습니다. 추가 매수도 매도도 하지 말고 내일 판정을 보세요.",
    }[v]
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | FLOW 삼성전자우·하이닉스",
        "이게 뭐 알림: 장마감 후 두 종목 외인·기관 수급으로 A/B/C를 다시 판정합니다.",
        f"지금 결과: {LABEL[v]} ({move})",
        f"- 삼성전자우 외인 {eok(sec['frgn_won'])} / 기관 {eok(sec['inst_won'])}",
        f"- 하이닉스 외인 {eok(hyx['frgn_won'])}",
        f"- 기준일 {st['base_date']} 대비: 우 외인 {eok(sec['frgn_won'] - base['sec_frgn'])}, 우 기관 {eok(sec['inst_won'] - base['sec_inst'])}, 닉스 외인 {eok(hyx['frgn_won'] - base['hyx_frgn'])}",
        f"지금 할 일: {do_now}",
        "나중에: B가 2영업일 연속이면 비중 확대를 검토합니다. A가 이어지면 관망 유지.",
        "익절/줄일 때: 이 알림만으로 팔지 않습니다. 보유 알림([SEC] 등) 기준을 따르세요.",
        "하지 말 것: 첫 B 하루만 보고 몰빵, C에서 급매수·급매도.",
        "",
        "금액=순매수 주식 수 x KRX 정규장 종가 추정. 15:40 수치는 잠정치라 저녁 확정치와 다를 수 있습니다.",
    ]
    msg = "\n".join(lines)
    print(msg)
    publish("cwally-port", f"[FLOW] 005935/000660 {LABEL[v]}", msg, 4 if v in ("A", "B") else 3,
            [{"A": "rotating_light", "B": "rocket", "C": "eyes"}[v]], CFG.get("ntfy_server"))

    st["last_day"], st["last_verdict"] = day, v
    st["history"] = (st.get("history") or [])[-19:] + [{**today, "verdict": v}]
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
