#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "nfp1002.json"
STATE = ROOT / "state" / "nfp1002.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html):
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t))


def num(token):
    if token is None:
        return None
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def parse_bls(text):
    payroll = None
    m = re.search(
        r"Total nonfarm payroll employment (increased|decreased|fell|edged down|changed little|was essentially unchanged)(?: by)? ([0-9,]+) in September",
        text,
        re.I,
    )
    if m:
        payroll = num(m.group(2))
        if payroll is not None and re.search(r"decreased|fell|edged down", m.group(1), re.I):
            payroll = -abs(payroll)
    if payroll is None:
        m = re.search(r"nonfarm payroll employment.{0,80}in September \(([+-]?[0-9,]+)\)", text, re.I)
        if m:
            payroll = num(m.group(1))
    unemp = None
    m = re.search(
        r"unemployment rate (?:was unchanged at|remained at|held at|edged (?:up|down) to|rose to|fell to|declined to|decreased to|increased to) ([0-9.]+) percent",
        text,
        re.I,
    )
    if m:
        unemp = num(m.group(1))
    ahe_mom = None
    m = re.search(
        r"average hourly earnings for all employees on private nonfarm payrolls (rose|increased|fell|decreased|were unchanged)(?: by [0-9]+ cents,)? or ([0-9.]+) percent",
        text,
        re.I,
    )
    if m:
        ahe_mom = num(m.group(2))
        if ahe_mom is not None and re.search(r"fell|decreased", m.group(1), re.I):
            ahe_mom = -abs(ahe_mom)
        if re.search(r"unchanged", m.group(1), re.I):
            ahe_mom = 0.0
    ahe_yoy = None
    m = re.search(r"Over the year, average hourly earnings have increased by ([0-9.]+) percent", text, re.I)
    if m:
        ahe_yoy = num(m.group(1))
    embargo = bool(re.search(r"EMBARGOED UNTIL", text, re.I) and re.search(r"October 2, 2026", text))
    published = bool(payroll is not None and not embargo)
    return {
        "published": published,
        "nfp": payroll,
        "unemp": unemp,
        "ahe_mom": ahe_mom,
        "ahe_yoy": ahe_yoy,
        "embargo": embargo,
    }


def yahoo(ticker):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1m&range=1d"
    try:
        payload = json.loads(fetch(url))
        meta = payload["chart"]["result"][0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        return (float(price) if price is not None else None, float(prev) if prev is not None else None)
    except Exception as exc:
        print("quote fail", ticker, exc)
        return None, None


def yield_pair(price, prev):
    def norm(v):
        if v is None:
            return None
        return v / 10.0 if v > 20 else v
    return norm(price), norm(prev)


def load_state():
    st = {"final_notified": False, "fail_notified": False, "empty_notified": False, "last_key": None, "parsed": {}}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st, now):
    st["updated_at"] = now.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fmt_k(v):
    if v is None:
        return "확인 불가"
    return f"{v/1000:.0f}천"


def fmt_pct(v):
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "확인 불가"


def surprise(actual, cons, unit):
    if actual is None or cons is None:
        return "비교불가"
    gap = actual - cons
    if unit == "k":
        if abs(gap) < 10000:
            return "부합"
        return ("상회" if gap > 0 else "하회") + f"({gap/1000:+.0f}천)"
    if abs(gap) < 0.05:
        return "부합"
    return ("상회" if gap > 0 else "하회") + f"({gap:+.1f}%p)"


def reaction(now_px, prev_px, kind):
    if now_px is None or prev_px is None:
        return "확인 불가"
    if kind == "bp":
        return f"{now_px:.2f}% (전일 {prev_px:.2f}, {(now_px-prev_px)*100:+.0f}bp)"
    chg = (now_px - prev_px) / prev_px * 100 if prev_px else None
    if chg is None:
        return "확인 불가"
    return f"{now_px:,.2f} ({chg:+.2f}%)"


def advice(parsed, tnx_bp):
    cons_hot = 0
    cons_soft = 0
    nfp, unemp, yoy = parsed.get("nfp"), parsed.get("unemp"), parsed.get("ahe_yoy")
    if nfp is not None and nfp >= 130000:
        cons_hot += 1
    if nfp is not None and nfp <= 50000:
        cons_soft += 1
    if unemp is not None and unemp <= 4.0:
        cons_hot += 1
    if unemp is not None and unemp >= 4.2:
        cons_soft += 1
    if yoy is not None and yoy >= 3.4:
        cons_hot += 1
    if yoy is not None and yoy <= 3.0:
        cons_soft += 1
    if cons_hot and not cons_soft:
        extra = " 10년물이 5bp 이상 올랐으면 추가매수 하지 마세요." if (tnx_bp is None or tnx_bp >= 5) else " 10년물이 안 올랐으면 금리 반응을 확인 후에만 보세요."
        return "추격매수 금지. SKHY, SOX 신규 매수 보류. 기존 반도체는 유지." + extra, "강한 고용"
    if cons_soft and not cons_hot:
        extra = " 10년물이 5bp 이상 내렸으면 급락분만 분할." if (tnx_bp is not None and tnx_bp <= -5) else " 금리 하락이 확인되기 전에는 사지 마세요."
        return "반등 추격 금지. 고용 한 방으로 비중 확대 금지." + extra, "약한 고용"
    if cons_hot and cons_soft:
        return "헤드라인과 임금이 엇갈림. 손대지 말고 10년물 방향을 먼저 보세요.", "혼조"
    return "컨센서스 부합. 오늘 매매하지 마세요. 기존 보유 유지.", "부합"


def emit(cfg, title, lines, pri):
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), pri, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def main():
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
    cons = cfg["consensus"]
    parsed, err = {}, ""
    try:
        parsed = parse_bls(textify(fetch(cfg["sources"]["bls"])))
    except Exception as exc:
        err = str(exc)
        print("bls fail", exc)
    tnx_now, tnx_prev = yahoo(cfg["tickers"]["tnx"])
    tnx_now, tnx_prev = yield_pair(tnx_now, tnx_prev)
    sox_now, sox_prev = yahoo(cfg["tickers"]["sox"])
    sk_now, sk_prev = yahoo(cfg["tickers"]["hynix"])
    tnx_bp = None if tnx_now is None or tnx_prev is None else (tnx_now - tnx_prev) * 100
    st["parsed"] = parsed
    st["quotes"] = {"tnx": [tnx_now, tnx_prev], "sox": [sox_now, sox_prev], "skhy": [sk_now, sk_prev]}
    if mode != "dry" and not parsed:
        if not st.get("fail_notified"):
            emit(cfg, "[NFP] 조회 실패 | 약한 고용으로 읽지 마", [
                now.strftime("%Y-%m-%d %H:%M KST") + " | NFP | 9월 고용",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + (err or "출처 없음") + ". 서프라이즈로 처리하지 않음.",
                "지금 할 일: BLS 원문을 직접 확인. 이 실패로 매매하지 마세요.",
                "나중에: 원문이 열리면 force로 한 번 더 보냅니다. 만료 2026-10-03.",
                "익절/줄일 때: 실패 알림으로 비중 조정 금지.",
                "하지 말 것: 조회 실패를 고용 부진으로 읽기.",
            ], 4)
            st["fail_notified"] = True
        save_state(st, now)
        return 0
    published = bool(parsed.get("published"))
    kind = "wait"
    send = False
    if published:
        send, kind = True, "final"
    elif mode in {"force", "dry"} or now >= datetime.fromisoformat("2026-10-02T22:45:00").replace(tzinfo=KST):
        send, kind = True, "empty"
    call, tag = advice(parsed, tnx_bp)
    title = "[NFP] 아직 9월 발표문 없음 | 대기"
    if kind == "final":
        title = f"[NFP] {tag} | {call.split('.')[0]}"
    status = (
        "고용 " + fmt_k(parsed.get("nfp")) + " " + surprise(parsed.get("nfp"), cons.get("nfp"), "k")
        + " / 실업 " + fmt_pct(parsed.get("unemp")) + " " + surprise(parsed.get("unemp"), cons.get("unemp"), "p")
        + " / 임금 전년비 " + fmt_pct(parsed.get("ahe_yoy")) + " " + surprise(parsed.get("ahe_yoy"), cons.get("ahe_yoy"), "p")
        + " / 임금 전월비 " + fmt_pct(parsed.get("ahe_mom")) + " " + surprise(parsed.get("ahe_mom"), cons.get("ahe_mom"), "p")
        + ". 컨센서스 고용 9만, 실업 4.1%, 임금 전년비 3.2%, 전월비 0.3(미단일)."
        + " 10년물 " + reaction(tnx_now, tnx_prev, "bp")
        + " / SOX " + reaction(sox_now, sox_prev, "pct")
        + " / SKHY " + reaction(sk_now, sk_prev, "pct") + "."
    )
    if kind != "final":
        status = "9월 발표문이 아직 아님. 약한 고용으로 처리하지 않음. " + status
        call = "발표 확인 전 포지션 유지. 이 대기를 매수 신호로 읽지 마세요."
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | NFP | 9월 고용보고서",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + call,
        "나중에: 같은 결과는 다시 안 보냄. 만료 2026-10-03. 이 신호는 당일 반응만.",
        "익절/줄일 때: SOX, SKHY가 반등해도 고용이 강하면 추격 익절 금지. 약한 고용에 금리가 안 내리면 분할 매수 금지.",
        "하지 말 것: 신용, 미수, 추격매수. 블루버그 8.5만 컨센서스를 이 숫자와 같은 잠금으로 쓰기. 임금 전월비 0.3은 단일 잠금이 아님.",
        "출처: " + cfg["sources"]["bls"] + " / " + cons.get("source", ""),
    ]
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        save_state(st, now)
        return 0
    if not send:
        print("condition not met; silent")
        save_state(st, now)
        return 0
    emit(cfg, title[:180], lines, 4 if kind == "final" else 3)
    st["final_notified" if kind == "final" else "empty_notified"] = True
    st["last_key"] = json.dumps(parsed, sort_keys=True)
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
