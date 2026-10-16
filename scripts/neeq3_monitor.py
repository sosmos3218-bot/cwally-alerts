#!/usr/bin/env python3
"""NEE Q3 2026 one-shot: EPS/guidance, price reaction, Dominion merger note, valuation check.
Not investment advice.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "neeq3.json"
STATE = ROOT / "state" / "neeq3.json"
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}


def fetch(url):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore"), resp.geturl()


def textify(html):
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t))


def load_state():
    st = {"notified": False, "fail_notified": False}
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


def first(pattern, text):
    m = re.search(pattern, text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def collect(cfg):
    blobs, errors = [], []
    try:
        raw, _ = fetch(cfg["sources"]["news"])
        blobs.append(textify(raw))
    except Exception as exc:
        errors.append(f"news: {exc}")
    try:
        raw, _ = fetch(cfg["sources"]["sec"])
        data = json.loads(raw)
        recent = data.get("filings", {}).get("recent", {})
        for form, date, acc, doc in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
        ):
            if date >= "2026-10-21" and form in {"8-K", "EX-99.1"}:
                url = f"https://www.sec.gov/Archives/edgar/data/753308/{acc.replace('-', '')}/{doc}"
                try:
                    body, _ = fetch(url)
                    blobs.append(textify(body))
                except Exception as exc:
                    errors.append(f"secfile: {exc}")
                break
    except Exception as exc:
        errors.append(f"sec: {exc}")
    return "\n".join(blobs), "; ".join(errors)


def get_price(cfg):
    try:
        raw, _ = fetch(cfg["sources"]["quote"])
        data = json.loads(raw)
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None, "시세 없음"
        quote = result[0]
        meta = quote.get("meta") or {}
        price = meta.get("regularMarketPrice")
        return float(price) if price else None, f"현재 {price}"
    except Exception as exc:
        return None, f"시세 실패: {exc}"


def parse(text):
    eps = first(r"(?:adjusted |diluted )?EPS of \$([0-9.]+)", text) or first(r"earnings per share of \$([0-9.]+)", text)
    guide_low = first(r"\$([0-9.]+) to \$[0-9.]+ per share", text)
    guide_high = first(r"\$[0-9.]+ to \$([0-9.]+) per share", text)
    guide_up = bool(re.search(r"(raised|increased).{0,30}(guidance|outlook)", text, re.I))
    guide_cut = bool(re.search(r"(lowered|reduced).{0,30}(guidance|outlook)", text, re.I))
    merger = bool(re.search(r"Dominion.{0,50}(approval|regulatory|merger)", text, re.I))
    published = bool(re.search(r"third.quarter|Q3 2026", text, re.I) and eps)
    return {
        "published": published,
        "eps": eps,
        "guide_low": guide_low,
        "guide_high": guide_high,
        "guide_up": guide_up,
        "guide_cut": guide_cut,
        "merger": merger,
    }


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
        print("outside window; silent")
        save_state(st, now)
        return 0
    if st.get("notified") and mode != "force":
        print("already notified; skip")
        save_state(st, now)
        return 0
    text, err = collect(cfg)
    parsed = parse(text) if text else {}
    price, price_note = get_price(cfg)
    b = cfg["baseline"]
    if mode == "dry":
        print("DRY RUN", parsed, price_note, err)
        save_state(st, now)
        return 0
    if not parsed.get("published"):
        if not text and not st.get("fail_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | NEE | Q3 실적",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + (err or "없음"),
                "지금 할 일: IR 직접 확인. 이 알림으로 매매 금지.",
                "나중에: 22:40, 23:40 재확인.",
                "익절/줄일 때: 조회 실패로 조정 금지.",
                "하지 말 것: 실패를 미스로 단정. 신용/미수/추격.",
            ]
            publish(resolve_topic("port"), "[NEE] 조회 실패 | 보류", "\n".join(lines), 4)
            st["fail_notified"] = True
            save_state(st, now)
            return 0
        print("not published yet")
        save_state(st, now)
        return 0
    # scoring
    action = "보유"
    why = "기본 유지"
    if parsed.get("guide_cut"):
        action = "주의"
        why = "가이던스 하향"
    elif parsed.get("guide_up") or (parsed.get("guide_high") and parsed["guide_high"] > b["guidance_high"]):
        action = "관심"
        why = "가이던스 상향 또는 유지"
    if price and price < b["pre_price"] * 0.97:
        action = "관심" if action != "주의" else action
        why += ". 주가 약세 반응"
    val = ""
    if price and b.get("analyst_target"):
        up = (b["analyst_target"] / price - 1) * 100
        val = f"현재 {price:.2f}, 목표가 {b['analyst_target']} ({up:.1f}% 여력)"
    merger_note = "합병 언급 있음" if parsed.get("merger") else "합병 업데이트 미확인"
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | NEE | Q3 판정",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 상태: {action}. {why}. EPS {parsed.get('eps') or '미확인'}. 가이던스 {parsed.get('guide_low')}-{parsed.get('guide_high') or ''}. {merger_note}. {val}. {price_note}",
        f"지금 할 일: {action}. 확인불가는 통과로 채우지 말 것.",
        "나중에: 일회 창 종료. 합병 규제 승인 지속 모니터링 권장.",
        "익절/줄일 때: 가이던스 하향 또는 주가 급락 시 재평가.",
        "하지 말 것: 단기 반응만으로 추격. 신용/미수. 투자 권유 아님.",
    ]
    title = f"[NEE] {action} | Q3 재평가"
    publish(resolve_topic("port"), title, "\n".join(lines), 4 if action != "보유" else 3)
    st["notified"] = True
    st["score"] = action
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
