#!/usr/bin/env python3
"""Zscaler 2026-10-06 Investor Day oneshot. Year-guarded. Sends at most one final."""
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
CFG = ROOT / "configs" / "zs1006.json"
STATE = ROOT / "state" / "zs1006.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore"), resp.geturl()


def textify(html):
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t))


def links(html):
    out = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
        if href.startswith("/"):
            href = "https://ir.zscaler.com" + href
        if "investor-day" in href or "2026-10-0" in href or "october-6" in href:
            out.append(href.split("#")[0])
    return list(dict.fromkeys(out))[:4]


def num(token):
    try:
        return float(token.replace(",", ""))
    except (TypeError, ValueError):
        return None


def money_b(token, unit):
    v = num(token)
    if v is None:
        return None
    unit = (unit or "").lower()
    if unit.startswith("m"):
        return v / 1000.0
    if v > 100:
        return v / 1000.0
    return v


def yahoo(ticker):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1d&range=5d"
    try:
        payload = json.loads(fetch(url)[0])
        meta = payload["chart"]["result"][0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        return (float(price) if price is not None else None, float(prev) if prev is not None else None)
    except Exception as exc:
        print("quote fail", exc)
        return None, None


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


def parse_doc(text):
    arr = []
    for m in re.finditer(
        r"(?:ARR|annual recurring revenue)[^.]{0,140}?\$([0-9]+(?:\.[0-9]+)?)\s*(billion|million|bn|m)",
        text,
        re.I,
    ):
        arr.append(money_b(m.group(1), m.group(2)))
    growth = []
    for m in re.finditer(
        r"(?:ARR|annual recurring revenue)[^.]{0,80}?([0-9]{1,2}(?:\.[0-9]+)?)\s*(?:%|percent)",
        text,
        re.I,
    ):
        growth.append(num(m.group(1)))
    ai_dollars = []
    for m in re.finditer(
        r"(?:security for AI|AI security)[^.]{0,160}?\$([0-9]+(?:\.[0-9]+)?)\s*(billion|million|bn|m)",
        text,
        re.I,
    ):
        ai_dollars.append(money_b(m.group(1), m.group(2)))
    ai_mention = bool(re.search(r"security for AI|AI security", text, re.I))
    margins = []
    for m in re.finditer(
        r"(operating margin|free cash flow margin|FCF margin)[^.]{0,80}?([0-9]{2}(?:\.[0-9]+)?)\s*(?:%|percent)",
        text,
        re.I,
    ):
        margins.append((m.group(1).lower(), num(m.group(2))))
    long_term = bool(re.search(r"long-term|target model|fiscal 2028|fiscal 2029|FY28|FY29", text, re.I))
    outlook = bool(re.search(r"outlook|guidance|fiscal 2027|FY27|raised|increased our", text, re.I))
    eventish = bool(re.search(r"Investor Day", text, re.I)) and outlook
    return {
        "arr_b": [v for v in arr if v and 3 < v < 8],
        "growth": [v for v in growth if v and 5 <= v <= 40],
        "ai_b": [v for v in ai_dollars if v and 0.05 <= v <= 3],
        "ai_mention": ai_mention,
        "margins": [(k, v) for k, v in margins if v and 10 <= v <= 45],
        "long_term": long_term,
        "eventish": eventish,
    }


def classify(parsed, base, rules):
    arrs = parsed.get("arr_b") or []
    growths = parsed.get("growth") or []
    ais = parsed.get("ai_b") or []
    margins = parsed.get("margins") or []
    fcf = [v for k, v in margins if "cash" in k or "fcf" in k]
    opm = [v for k, v in margins if "operating" in k]
    arr_high = max(arrs) if arrs else None
    growth = max(growths) if growths else None
    reasons = []
    verdict = "유지"
    if growth is not None and growth < rules["abandon_growth_pct"]:
        verdict = "폐기"
        reasons.append(f"ARR 성장 {growth:.1f}%가 폐기선 {rules['abandon_growth_pct']:.0f}% 아래")
    elif arr_high is not None and arr_high < base["arr_low_b"] - 0.05:
        verdict = "폐기"
        reasons.append(f"ARR 상단 {arr_high:.3f}십억달러가 기존 하단 {base['arr_low_b']:.3f} 아래")
    else:
        if arr_high is not None and arr_high > rules["strengthen_arr_high_b"] + 0.01:
            verdict = "강화"
            reasons.append(f"ARR 가이던스 {arr_high:.3f}십억달러, 기존 상단 {base['arr_high_b']:.3f} 상회")
        if growth is not None and growth > rules["strengthen_growth_pct"]:
            verdict = "강화"
            reasons.append(f"ARR 성장 {growth:.1f}%, 기존 {base['arr_growth_pct']:.1f}% 상회")
        if ais:
            verdict = "강화"
            reasons.append(f"AI 보안 금액 목표 {max(ais):.2f}십억달러 제시")
        if fcf and max(fcf) >= rules["strengthen_fcf_pct"]:
            verdict = "강화"
            reasons.append(f"FCF 마진 {max(fcf):.1f}%")
    if not reasons:
        if not arrs and not growths and not ais and not margins:
            return "미확인", "가이던스 숫자 없음"
        reasons.append("기존 FY2027 밴드 안. AI 매출 목표 또는 장기 마진 상향 없음")
    return verdict, "; ".join(reasons)


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
        print("outside 2026-10-07 window; silent")
        save_state(st, now)
        return 0
    if st.get("final_notified") and mode != "force":
        print("already sent final; skip")
        save_state(st, now)
        return 0
    base = cfg["baseline"]
    rules = cfg["rules"]
    texts, errors, urls = [], [], []
    for key, url in cfg["sources"].items():
        try:
            html, final = fetch(url)
            urls.append(final)
            texts.append(textify(html))
            if key == "press":
                for href in links(html):
                    if href in urls:
                        continue
                    try:
                        h2, f2 = fetch(href)
                        urls.append(f2)
                        texts.append(textify(h2))
                    except Exception as exc:
                        errors.append(f"{href}: {exc}")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            print("fetch fail", url, exc)
    blob = "\n".join(texts)
    parsed = parse_doc(blob) if blob else {}
    parsed["eventish"] = bool(parsed.get("eventish")) or ("investor day" in blob.lower() and "outlook" in blob.lower())
    price, prev = yahoo("ZS")
    st["parsed"] = parsed
    st["price"] = [price, prev]
    st["errors"] = errors[:6]
    published = bool(parsed.get("arr_b") or parsed.get("growth") or parsed.get("ai_b") or parsed.get("margins")) and parsed.get("eventish")
    # Pre-event announcement page also contains outlook words. Require a post-event marker.
    post = bool(re.search(r"October 6, 2026|Oct\.? 6, 2026", blob)) and bool(
        re.search(r"raised|long-term target|target model|today announced|commentary", blob, re.I)
    )
    if not post:
        published = False
    verdict, why = classify(parsed, base, rules)
    if price is not None and price <= rules["abandon_price"] and published:
        verdict = "폐기"
        why = f"주가 {price:.2f}달러가 폐기선 {rules['abandon_price']:.0f} 이하. " + why
    kind = "wait"
    send = False
    if not texts and errors:
        kind = "fail"
        send = mode != "dry" and not st.get("fail_notified")
    elif published:
        kind, send = "final", True
    elif mode in {"force", "dry"} or in_win or mode == "force":
        kind = "empty"
        send = not st.get("empty_notified")
    px = "확인 불가" if price is None else f"{price:.2f}달러"
    if prev:
        px += f" (전일 {prev:.2f})"
    arr_txt = "미확인" if not parsed.get("arr_b") else ", ".join(f"{v:.3f}십억" for v in parsed["arr_b"][:3])
    gr_txt = "미확인" if not parsed.get("growth") else ", ".join(f"{v:.1f}%" for v in parsed["growth"][:3])
    ai_txt = "금액 목표 없음" if not parsed.get("ai_b") else ", ".join(f"{v:.2f}십억달러" for v in parsed["ai_b"][:2])
    if parsed.get("ai_mention") and not parsed.get("ai_b"):
        ai_txt = "언급만 있고 매출 목표 숫자 없음"
    mg_txt = "미확인" if not parsed.get("margins") else ", ".join(f"{k} {v:.1f}%" for k, v in parsed["margins"][:3])
    if kind == "fail":
        title = "[ZS] 조회 실패 | 발표로 읽지 마"
        status = "IR 조회 실패. 논지 변경으로 처리하지 않음. " + "; ".join(errors[:2])
        call = "196달러 1차만 유지. 2차 보류. 실패를 폐기나 강화로 읽지 마세요."
    elif kind == "final":
        action = {"유지": "2차는 가격 확인 후", "강화": "2차 분할 진행", "폐기": "추가매수 중단", "미확인": "2차 보류"}[verdict]
        title = f"[ZS] 논지 {verdict} | {action}"
        status = (
            f"FY2027 ARR {arr_txt} / 성장 {gr_txt}. 기존 43.96~44.26억달러, 약 17%. "
            f"AI 보안 {ai_txt}. 기존은 수주만 +50%이고 매출 목표 없음. 마진 {mg_txt}. "
            f"기존 영업이익률 약 23.7%, FCF 마진 23~23.5%, 장기 목표 미공개. ZS {px}."
        )
        if verdict == "강화":
            call = "196달러 논지 강화. 남은 2/3를 나눠 진행. 추격은 220달러 위에서 하지 마세요. 근거: " + why
        elif verdict == "폐기":
            call = "196달러 분할매수 논지 폐기. 추가매수 중단. 보유분은 별도. 근거: " + why
        else:
            call = "196달러 논지 유지. 1차는 유지, 2차는 보류하거나 196달러 아래에서만. 근거: " + why
    else:
        title = "[ZS] 자료 없음 | 196달러 1차만, 2차 보류"
        status = (
            "행사 후 가이던스 숫자가 아직 없음. 미발표를 상향으로 처리하지 않음. "
            f"기존 ARR 43.96~44.26억달러(약 17%), AI 매출 목표 없음, 장기 마진 목표 없음. ZS {px}."
        )
        call = "논지 유지. 2차 매수는 보류. 15:30 KST 한 번만 재확인합니다."
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | ZS | 10월 6일 인베스터 데이",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + call,
        "나중에: 최종 판단은 한 번만. 만료 2026-10-07. 2027년 이후 침묵.",
        "익절/줄일 때: 220달러 위 추격 익절 금지. 170달러 아래면 논지와 별개로 추가매수 중단.",
        "하지 말 것: 신용, 미수, 추격매수. 자료 없음이나 조회 실패를 상향으로 읽기.",
        "출처: " + cfg["sources"]["press"],
    ]
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("KIND", kind, "PUBLISHED", published, "PARSED", json.dumps(parsed, ensure_ascii=False))
        save_state(st, now)
        return 0
    if not send:
        print("condition not met or already sent; silent")
        save_state(st, now)
        return 0
    emit(cfg, title[:180], lines, 4 if kind == "final" else 3)
    if kind == "final":
        st["final_notified"] = True
    elif kind == "fail":
        st["fail_notified"] = True
    else:
        st["empty_notified"] = True
    st["last_key"] = kind + ":" + verdict
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
