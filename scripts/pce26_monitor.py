#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "pce26.json"
STATE = ROOT / "state" / "pce26.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\\s+", " ", unescape(t))


def pct(text: str, pats):
    for pat in pats:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def signed(text: str, needle: str):
    m = re.search(needle, text, re.I)
    if not m:
        return None
    chunk = text[m.start(): m.start() + 180]
    num = re.search(r"([0-9.]+)\\s*percent", chunk, re.I)
    if not num:
        return None
    val = float(num.group(1))
    if re.search(r"decreased|fell|down", chunk, re.I):
        return -abs(val)
    if re.search(r"unchanged", chunk, re.I):
        return 0.0
    return val


def parse_bea(text: str) -> dict:
    yoy_h = pct(text, [r"PCE price index increased ([0-9.]+) percent from one year ago"])
    yoy_c = pct(text, [r"Excluding food and energy, the PCE price index increased ([0-9.]+) percent from one year ago"])
    mom_h = signed(text, r"From the preceding month, the PCE price index for August")
    mom_c = signed(text, r"Excluding food and energy, the PCE price index")
    income = signed(text, r"Personal income (?:increased|decreased|was unchanged)")
    spend = signed(text, r"personal consumption expenditures \\(PCE\\)")
    embargo = bool(re.search(r"EMBARGOED UNTIL RELEASE AT 8:30 a\\.m\\. EDT, Wednesday, September 30", text))
    published = bool(re.search(r"Personal Income and Outlays, August 2026", text, re.I) and (yoy_c or yoy_h) and not embargo)
    return {"published": published, "head_yoy": yoy_h, "core_yoy": yoy_c, "mom_head": mom_h, "mom_core": mom_c, "income_mom": income, "spend_mom": spend}


def last_fred(url: str, min_date: str):
    rows = []
    for line in fetch(url).splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2 or parts[1].strip() in {".", ""}:
            continue
        try:
            rows.append((parts[0].strip(), float(parts[1].strip())))
        except ValueError:
            continue
    rows = [r for r in rows if r[0] >= min_date]
    return rows[-1] if rows else None


def load_state():
    st = {"final_notified": False, "fail_notified": False, "empty_notified": False, "audit_notified": False, "last_key": None, "parsed": {}}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st, now):
    st["updated_at"] = now.isoformat()
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fmt(v):
    return f"{v:.2f}%" if isinstance(v, (int, float)) else "확인 불가"


def delta(new, old):
    if new is None or old is None:
        return "확인 불가"
    return f"{new-old:+.2f}%p"


def surprise(actual, cons):
    if actual is None or cons is None:
        return "비교불가"
    gap = round(actual - cons, 2)
    if abs(gap) < 0.05:
        return "부합"
    return f"{상회 if gap > 0 else 하회}({gap:+.2f}%p)"


def emit(cfg, title, lines, pri):
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), pri, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    in_win = start <= now <= expire and now.year == int(cfg.get("event_year", 2026))
    print(f"{now.isoformat()} mode={mode} window={in_win}")
    if mode not in {"force", "dry"} and not in_win:
        print("outside 2026 window; silent")
        save_state(st, now)
        return 0
    if st.get("final_notified") and mode != "force":
        print("already sent final; skip")
        save_state(st, now)
        return 0
    pre = cfg["pre_revision"]
    cons = cfg.get("consensus") or {}
    bea, bea_ok, bea_err = {}, False, ""
    try:
        bea = parse_bea(textify(fetch(cfg["sources"]["bea"])))
        bea_ok = True
    except Exception as exc:
        bea_err = str(exc)
        print(f"bea fail: {exc}")
    fred_c = fred_h = None
    try:
        fred_c = last_fred(cfg["sources"]["fred_core"], "2026-07-01")
        fred_h = last_fred(cfg["sources"]["fred_head"], "2026-07-01")
    except Exception as exc:
        print(f"fred fail: {exc}")
    core = bea.get("core_yoy")
    head = bea.get("head_yoy")
    if core is None and fred_c and fred_c[0] >= "2026-08-01":
        core = fred_c[1]
    if head is None and fred_h and fred_h[0] >= "2026-08-01":
        head = fred_h[1]
    published = bool(bea.get("published") or (core is not None and fred_c and fred_c[0] >= "2026-08-01"))
    parsed = {"core_yoy": core, "head_yoy": head, "mom_core": bea.get("mom_core"), "mom_head": bea.get("mom_head"), "income_mom": bea.get("income_mom"), "spend_mom": bea.get("spend_mom"), "published": published}
    st["parsed"] = parsed
    st["bea_ok"] = bea_ok
    if mode != "dry" and not bea_ok and fred_c is None and fred_h is None:
        if not st.get("fail_notified"):
            emit(cfg, "[PCE26] 조회 실패 | 재시도", [f"{now.strftime('%Y-%m-%d %H:%M KST')} | PCE26 | 8월 PCE", f"이게 뭐 알림: {cfg['explain']}", f"지금 상태: 조회 실패. {bea_err or '출처 없음'}. 둔화로 처리하지 않음.", "지금 할 일: 다음 기존 슬롯까지 대기.", "나중에: 원문이 열리면 한 번만 보냅니다. 만료 2026-10-02.", "익절/줄일 때: 실패 알림으로 매매하지 마세요.", "하지 말 것: 조회 실패를 물가 둔화로 읽기."], 4)
            st["fail_notified"] = True
        save_state(st, now)
        return 0
    last_day = now.date() == expire.date()
    kind = "wait"
    send = False
    if published:
        send, kind = True, "final"
    elif last_day or mode in {"force", "dry"}:
        send, kind = True, ("final" if published else "empty")
    title = "[PCE26] 아직 결과 없음 | 대기"
    if kind == "final" and core is not None:
        if core >= 3.20:
            title = f"[PCE26] 근원 {fmt(core)} | 10월 인하 보류"
        elif core <= 3.05:
            title = f"[PCE26] 근원 {fmt(core)} | 인하 단정 금지"
        else:
            title = f"[PCE26] 근원 {fmt(core)} | 동결 쪽 유지"
    elif kind == "empty":
        title = "[PCE26] 발표문 없음 | 대기"
    if kind == "final":
        status = f"헤드 {fmt(head)} / 근원 {fmt(core)} / m/m 헤드 {fmt(parsed['mom_head'])} 근원 {fmt(parsed['mom_core'])}. 소득 {fmt(parsed['income_mom'])} / 소비 {fmt(parsed['spend_mom'])}. 7월 근원 {pre['july_core_yoy']}% 대비 {delta(core, pre['july_core_yoy'])}. 서프라이즈({cons.get('source','미잠금')}): 근원YoY {surprise(core, cons.get('core_yoy'))} / 헤드YoY {surprise(head, cons.get('head_yoy'))} / 근원MoM {surprise(parsed['mom_core'], cons.get('core_mom'))} / 헤드MoM {surprise(parsed['mom_head'], cons.get('head_mom'))} / 소득 {surprise(parsed['income_mom'], cons.get('income_mom'))} / 소비 {surprise(parsed['spend_mom'], cons.get('spend_mom'))}."
        do_now = "숫자는 공시 원문만. 10/28 인하로 바로 읽지 마세요."
    else:
        status = "8월 수치가 아직 안 열림. 물가 둔화로 처리하지 않음."
        do_now = "발표 확인 전 포지션 유지."
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | PCE26 | 8월 PCE + 연례개정", f"이게 뭐 알림: {cfg['explain']}", f"지금 상태: {status}", f"지금 할 일: {do_now}", "나중에: 같은 결과는 다시 안 보냄. 만료 2026-10-02. 9월 PCE는 10/29.", "익절/줄일 때: 근원 0.2%p 하락만으로 추격 매수/인하 베팅 금지.", "하지 말 것: 개정을 실물 급락으로 읽기. 컨센서스 미잠금 항목을 서프라이즈로 단정.", f"출처: {cfg['sources']['bea']}"]
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        save_state(st, now)
        return 0
    if not send:
        print("condition not met; silent")
        save_state(st, now)
        return 0
    emit(cfg, title, lines, 4 if kind == "final" else 3)
    st["final_notified" if kind == "final" else "empty_notified"] = True
    st["last_key"] = json.dumps(parsed, sort_keys=True)
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
