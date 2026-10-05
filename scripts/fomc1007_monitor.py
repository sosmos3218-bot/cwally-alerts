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
CFG = ROOT / "configs" / "fomc1007.json"
STATE = ROOT / "state" / "fomc1007.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

HAWK = (
    "further increase",
    "additional firming",
    "additional tightening",
    "another increase",
    "raise the target",
    "higher for longer",
    "upside risks to inflation",
    "more persistent",
    "not sufficiently restrictive",
    "further policy firming",
)
DOVE = (
    "slower pace",
    "slow the pace",
    "pause",
    "hold the target",
    "maintain the target",
    "wait for more",
    "downside risks to employment",
    "labor market cooling",
    "cooling in the labor",
    "look through",
    "transitory",
    "temporary energy",
    "preferred to leave",
    "preferred no change",
)


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore"), resp.geturl()


def textify(html):
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def load_state():
    st = {"final_notified": False, "fail_notified": False, "empty_notified": False, "last_key": None}
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


def yahoo(ticker):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1m&range=1d"
    try:
        payload = json.loads(fetch(url)[0])
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


def reaction(now_px, prev_px, kind):
    if now_px is None or prev_px is None:
        return "확인 불가"
    if kind == "bp":
        return f"{now_px:.2f}% (전일 {prev_px:.2f}, {(now_px - prev_px) * 100:+.0f}bp)"
    chg = (now_px - prev_px) / prev_px * 100 if prev_px else None
    if chg is None:
        return "확인 불가"
    return f"{now_px:,.2f} ({chg:+.2f}%)"


def weight(sentence):
    s = sentence.lower()
    if re.search(r"\b(most|many) participants\b", s):
        return 2.0
    if re.search(r"\b(several|a number of) participants\b", s):
        return 1.5
    if re.search(r"\b(a few|one or two) participants\b", s):
        return 0.5
    if re.search(r"\bsome participants\b", s):
        return 1.0
    return 1.0


def classify(text):
    sentences = re.split(r"(?<=[.])\s+", text)
    buckets = {"hike": [], "oil": [], "jobs": []}
    hawk = dove = 0.0
    for sent in sentences:
        low = sent.lower()
        if not re.search(r"participant|committee|members", low):
            continue
        topic = None
        if re.search(r"further increase|additional firming|target range|tightening|rate increase|policy firming", low):
            topic = "hike"
        elif re.search(r"oil|energy price|crude|gasoline", low):
            topic = "oil"
        elif re.search(r"employment|labor market|job gains|unemployment|payroll", low):
            topic = "jobs"
        if topic is None:
            continue
        w = weight(sent)
        h = any(k in low for k in HAWK)
        d = any(k in low for k in DOVE)
        if topic == "oil" and re.search(r"persistent|second-round|pass[- ]through|not temporary", low):
            h = True
        if topic == "oil" and re.search(r"look through|temporary|supply shock|transitory", low):
            d = True
        if topic == "jobs" and re.search(r"still solid|consistent with|not enough|did not warrant", low):
            h = True
        if topic == "jobs" and re.search(r"cooling|slowing|softening|downside", low):
            d = True
        if h and not d:
            hawk += w
            side = "매파"
        elif d and not h:
            dove += w
            side = "비둘기"
        else:
            side = "중립"
        if len(buckets[topic]) < 2:
            buckets[topic].append(side + ": " + sent[:180])
    return hawk, dove, buckets


def stance(hawk, dove):
    if hawk == 0 and dove == 0:
        return "확인 불가", "문장 점수가 0. 매파나 비둘기로 읽지 마세요."
    if hawk >= dove * 1.25 and hawk > 0:
        return "매파", "추가 인상 여지 우위. 다우 추격 금지. 반도체 신규 매수 보류. 2년물이 내리기 전에는 전통업종 분할도 금지."
    if dove >= hawk * 1.25 and dove > 0:
        return "비둘기", "속도 조절 우위. 그래도 2년물이 실제로 내리기 전에는 다우 반등을 사지 마세요. 반도체 추격 금지."
    return "혼조", "의견이 갈림. 손대지 마세요. 10월 9일 미시간 기대 인플레를 기다리세요."


def emit(cfg, title, lines, pri):
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        "\n".join(lines),
        pri,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )


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
    text, url, err = "", "", ""
    for candidate in cfg["sources"]["minutes"]:
        try:
            html, url = fetch(candidate)
            text = textify(html)
            if re.search(r"Minutes of the Federal Open Market Committee", text, re.I) and re.search(r"September", text, re.I):
                err = ""
                break
            text, url = "", candidate
            err = "페이지는 열렸으나 9월 의사록 본문이 아님"
        except Exception as exc:
            err = str(exc)
            print("minutes fail", candidate, exc)
    dji_now, dji_prev = yahoo(cfg["tickers"]["dji"])
    sox_now, sox_prev = yahoo(cfg["tickers"]["sox"])
    y_now, y_prev = yahoo(cfg["tickers"]["us2y"])
    y_now, y_prev = yield_pair(y_now, y_prev)
    tnx_now, tnx_prev = yahoo(cfg["tickers"]["tnx"])
    tnx_now, tnx_prev = yield_pair(tnx_now, tnx_prev)
    st["quotes"] = {"dji": [dji_now, dji_prev], "sox": [sox_now, sox_prev], "us2y": [y_now, y_prev], "tnx": [tnx_now, tnx_prev]}
    if not text:
        st["last_error"] = err or "원문 없음"
        if mode == "dry":
            print("dry empty", err)
            save_state(st, now)
            return 0
        if not st.get("fail_notified"):
            emit(cfg, "[FLOW] 의사록 조회 실패 | 매매 금지", [
                now.strftime("%Y-%m-%d %H:%M KST") + " | FLOW | 9월 FOMC 의사록",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + (err or "출처 없음") + ". 매파나 비둘기로 처리하지 않음.",
                "지금 할 일: 연준 원문을 직접 확인. 이 실패로 다우, 반도체, 금리 포지션을 바꾸지 마세요.",
                "나중에: 09:10 KST 재시도가 원문을 보면 최종 알림을 보냅니다. 만료 2026-10-08.",
                "익절/줄일 때: 실패 알림으로 비중 조정 금지.",
                "하지 말 것: 조회 실패를 비둘기 신호로 읽기. 신용, 미수, 추격매수.",
            ], 4)
            st["fail_notified"] = True
        save_state(st, now)
        return 0
    hawk, dove, buckets = classify(text)
    tag, call = stance(hawk, dove)
    total = hawk + dove
    mix = "점수 없음" if total == 0 else f"매파 {hawk / total * 100:.0f}% / 비둘기 {dove / total * 100:.0f}% (가중 {hawk:.1f}:{dove:.1f})"
    bits = []
    for key, label in (("hike", "추가인상"), ("oil", "유가"), ("jobs", "고용")):
        bits.append(label + " " + ("; ".join(buckets[key]) if buckets[key] else "해당 문장 없음"))
    status = (
        mix + ". " + " ".join(bits)
        + " 다우 " + reaction(dji_now, dji_prev, "pct")
        + " / 반도체 " + reaction(sox_now, sox_prev, "pct")
        + " / 2년물선물 " + reaction(y_now, y_prev, "bp")
        + " / 10년물 " + reaction(tnx_now, tnx_prev, "bp") + "."
    )
    title = f"[FLOW] 의사록 {tag} | {call.split('.')[0]}"
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | FLOW | 9월 FOMC 의사록",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + call + " 기준선: " + cfg["judgment"]["baseline"],
        "나중에: 같은 결과는 다시 안 보냄. 만료 2026-10-08. 10월 9일 미시간 기대 인플레가 변경 조건.",
        "익절/줄일 때: 의사록이 매파면 다우 반등 익절 금지. 비둘기여도 2년물이 안 내리면 분할 매수 금지.",
        "하지 말 것: 신용, 미수, 반도체 추격. 문장 점수를 표결 수로 읽기. 키워드 분류는 원문 대체가 아님.",
        "출처: " + (url or cfg["sources"]["minutes"][0]),
    ]
    st["parsed"] = {"hawk": hawk, "dove": dove, "tag": tag, "url": url}
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        save_state(st, now)
        return 0
    emit(cfg, title[:180], lines, 4)
    st["final_notified"] = True
    st["last_key"] = tag
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
