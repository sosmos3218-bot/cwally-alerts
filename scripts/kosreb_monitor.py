#!/usr/bin/env python3
from __future__ import annotations
import json, re, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic
KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "kosreb.json"
STATE = ROOT / "state" / "kosreb.json"
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}

def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")

def yahoo(ticker):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1d&range=5d"
    try:
        payload = json.loads(fetch(url))
        meta = payload["chart"]["result"][0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        return (float(price) if price is not None else None, float(prev) if prev is not None else None)
    except Exception as exc:
        print("quote fail", ticker, exc)
        return None, None

def reaction(now_px, prev_px):
    if now_px is None or prev_px is None or prev_px == 0:
        return "확인 불가"
    chg = (now_px - prev_px) / prev_px * 100
    return f"{now_px:,.2f} ({chg:+.2f}%)"

def load_state():
    st = {"sent": False}
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

def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = load_state()
    if mode not in {"force", "dry"} and st.get("sent"):
        print("already sent; skip")
        return 0
    if mode not in {"force", "dry"} and now.year != 2026:
        print("year guard; silent")
        return 0
    kospi_now, kospi_prev = yahoo("^KS11")
    sam_now, sam_prev = yahoo("005930.KS")
    hynix_now, hynix_prev = yahoo("000660.KS")
    # Simplified CPI note: full parse lives in cpi1014; here we report market reaction
    status = (
        "코스피 " + reaction(kospi_now, kospi_prev)
        + " / 삼성전자 " + reaction(sam_now, sam_prev)
        + " / SK하이닉스 " + reaction(hynix_now, hynix_prev)
        + ". 외국인 수급과 27~28년 컨센은 실적 시즌 후 확인 필요. 선행 PER은 저점 구간 유지 추정."
    )
    # Judgment based on previous Kiwoom analysis
    call = "단기 반등 시나리오는 낙폭 과대 되돌림으로 유지 가능. 중기 연속성은 CPI가 근원 둔화 쪽이고 반도체 실적 신뢰가 회복될 때 강화."
    title = "[KOSREB] 반등 시나리오 재평가 | 비중 유지 검토"
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | KOSREB | 키움 반등 재평가",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + call + " 추격 매도보다 기존 비중 유지 또는 분할 매수 검토. 투자 권유 아님.",
        "나중에: 3분기 실적 시즌에서 반도체 EPS 상향 여부 확인. 외국인 순매도 지속 시 반등 연속성 제한.",
        "익절/줄일 때: CPI 핫 서프라이즈 + 10년물 상승 + 반도체 추가 급락 시 비중 축소 검토.",
        "하지 말 것: 하루 반등으로 악재 종료 단정. 신용/미수 추격매수.",
    ]
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        return 0
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        "\n".join(lines),
        3,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )
    st["sent"] = True
    save_state(st, now)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
