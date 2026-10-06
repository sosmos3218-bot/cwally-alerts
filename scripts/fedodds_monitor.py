#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "fedodds.json"
STATE = ROOT / "state" / "fedodds.json"
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept": "application/json, text/plain, */*", "Referer": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"}


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore"), resp.geturl()


def load_state():
    st = {"minutes_notified": False, "cpi_notified": False, "fail_notified": {}}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    st.setdefault("fail_notified", {})
    return st


def save_state(st, now):
    st["updated_at"] = now.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def event_for(mode, now):
    if mode in {"minutes", "cpi"}:
        return mode
    return "cpi" if now >= datetime(2026, 10, 14, 21, 30, tzinfo=KST) else "minutes"


def walk_probs(node, out):
    if isinstance(node, dict):
        date = None
        for key in ("meetingDate", "date", "meeting", "label"):
            val = node.get(key)
            if isinstance(val, str) and re.search(r"2026", val):
                date = val
                break
        probs = []
        for key in ("probabilities", "rateProbs", "probs"):
            if isinstance(node.get(key), list):
                probs = node[key]
                break
        if date and probs:
            out.append((date, probs, node))
        for val in node.values():
            walk_probs(val, out)
    elif isinstance(node, list):
        for val in node:
            walk_probs(val, out)


def hold_hike(probs):
    hold = hike = None
    for item in probs:
        if not isinstance(item, dict):
            continue
        label = str(item.get("rate") or item.get("label") or item.get("target") or "")
        raw = item.get("probability") or item.get("prob") or item.get("value")
        try:
            p = float(str(raw).replace("%", ""))
        except (TypeError, ValueError):
            continue
        if p <= 1:
            p *= 100
        low = label.lower()
        if "3.75" in label and "4.00" in label:
            hold = p
        elif re.search(r"hold|unchanged|no change", low):
            hold = p
        elif re.search(r"hike|increase|\+25|\+50", low) or "4.00-4.25" in label or "4.25-4.50" in label:
            hike = (hike or 0) + p
    if hold is None and hike is not None:
        hold = max(0.0, 100 - hike)
    if hike is None and hold is not None:
        hike = max(0.0, 100 - hold)
    return hold, hike


def pick_meeting(rows, month):
    for date, probs, _node in rows:
        if month in date or (month == "10" and re.search(r"Oct", date, re.I)) or (month == "12" and re.search(r"Dec", date, re.I)):
            return date, hold_hike(probs)
    return None, (None, None)


def judge(oct_hold, dec_hike, band):
    lo, hi, dec_min = band
    if oct_hold is None or dec_hike is None:
        return "확인 불가", "확률을 숫자로 읽지 못함. 유지로 처리하지 않음."
    oct_ok = lo <= oct_hold <= hi
    dec_ok = dec_hike >= dec_min
    if oct_ok and dec_ok:
        return "유지", "10월 동결 밴드 안, 12월 인상 가격 유지. 포지션 변경 금지."
    if oct_hold > hi and dec_ok:
        return "동결 강화", "10월 동결이 밴드 상단을 넘음. 12월 인상은 유지. 인상 철회로 읽지 마세요."
    if oct_hold < lo and dec_ok:
        return "10월 흔들림", "10월 동결이 밴드 하단 아래. 12월 인상은 유지. 10월 인상 재가격 여부만 확인."
    if not dec_ok and oct_ok:
        return "12월 약화", "10월 동결 밴드는 유지, 12월 인상 가격이 약해짐. 연내 인상 철회로 단정 금지."
    return "둘 다 이탈", "10월 동결 밴드와 12월 인상 가격이 같이 이탈. 시세 확인 전 매매 금지."


def emit(cfg, title, lines, pri):
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title[:180], "\n".join(lines), pri, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    in_win = start <= now <= expire and now.year == int(cfg.get("event_year", 2026))
    event = event_for(mode, now)
    print(f"{now.isoformat()} mode={mode} event={event} window={in_win}")
    if mode not in {"force", "dry"} and not in_win:
        print("outside 2026 window; silent")
        save_state(st, now)
        return 0
    flag = "minutes_notified" if event == "minutes" else "cpi_notified"
    if st.get(flag) and mode not in {"force", "dry"}:
        print("already sent", event)
        save_state(st, now)
        return 0
    raw, url, err = "", "", ""
    rows = []
    for candidate in cfg["sources"]:
        try:
            raw, url = fetch(candidate)
            payload = json.loads(raw)
            walk_probs(payload, rows)
            if rows:
                err = ""
                break
            err = "JSON은 열렸으나 회의 확률 없음"
        except Exception as exc:
            err = str(exc)
            print("odds fail", candidate, exc)
    oct_date, (oct_hold, oct_hike) = pick_meeting(rows, "10")
    dec_date, (dec_hold, dec_hike) = pick_meeting(rows, "12")
    band = (cfg["baseline"]["october_hold_low"], cfg["baseline"]["october_hold_high"], cfg["baseline"]["december_hike_min"])
    tag, call = judge(oct_hold, dec_hike, band)
    label = "9월 FOMC 의사록 이후" if event == "minutes" else "9월 CPI 이후"
    def fmt(v):
        return f"{v:.1f}%" if isinstance(v, float) else "확인 불가"
    if not rows:
        title = "[FOMC] 확률 조회 실패 | 유지로 읽지 마"
        lines = [
            now.strftime("%Y-%m-%d %H:%M KST") + " | FOMC | " + label,
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 조회 실패. " + (err or "출처 없음") + ". 77~85% 유지로 처리하지 않음.",
            "지금 할 일: CME FedWatch를 직접 확인. 이 실패로 금리, 달러, 수출주 포지션을 바꾸지 마세요.",
            "나중에: 같은 사건 재시도가 숫자를 보면 판정을 보냅니다. 만료 2026-10-15.",
            "익절/줄일 때: 실패 알림으로 비중 조정 금지.",
            "하지 말 것: 조회 실패를 동결 유지나 인상 철회로 읽기.",
        ]
        if mode == "dry":
            print(title)
            print("\n".join(lines))
            save_state(st, now)
            return 0
        if not st["fail_notified"].get(event):
            emit(cfg, title, lines, 4)
            st["fail_notified"][event] = True
        save_state(st, now)
        return 0
    status = (
        f"10월 동결 {fmt(oct_hold)} (밴드 77~85, 인상 {fmt(oct_hike)}, {oct_date or '회의일 미확인'}). "
        f"12월 누적 인상 {fmt(dec_hike)} (유지 기준 60% 이상, 동결 {fmt(dec_hold)}, {dec_date or '회의일 미확인'}). "
        "기준 " + cfg["baseline"]["note"]
    )
    title = f"[FOMC] {label} {tag} | {call.split('.')[0]}"
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | FOMC | " + label,
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + call,
        "나중에: 의사록 판정은 10월 8일, CPI 판정은 10월 14~15일 각 1회. 만료 2026-10-15. 10월 FOMC 성명은 10월 29일 03:00 KST.",
        "익절/줄일 때: 밴드 유지면 익절, 축소 금지. 이탈이어도 2년물과 달러가 같이 움직이지 않으면 비중 조정 금지.",
        "하지 말 것: 선물 확률을 연준 결정으로 읽기. 신용, 미수, 추격매수.",
        "출처: " + (url or cfg["sources"][0]),
    ]
    st["parsed"] = {"event": event, "tag": tag, "oct_hold": oct_hold, "dec_hike": dec_hike, "url": url}
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        save_state(st, now)
        return 0
    emit(cfg, title, lines, 4)
    st[flag] = True
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
