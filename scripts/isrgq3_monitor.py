#!/usr/bin/env python3
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
CFG = ROOT / "configs" / "isrgq3.json"
STATE = ROOT / "state" / "isrgq3.json"
# SEC/BLS block anonymous UAs; they require a contact (https://www.sec.gov/os/accessing-edgar-data)
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
    st = {"final_notified": False, "empty_notified": False, "fail_notified": False, "expire_notified": False, "score": None}
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


def abs_url(href):
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://investor.intuitivesurgical.com" + href
    return "https://investor.intuitivesurgical.com/" + href


def collect(cfg):
    blobs = []
    errors = []
    links = []
    try:
        raw, _ = fetch(cfg["sources"]["news"])
        blobs.append(textify(raw))
        for href in re.findall(r'href="([^"]+)"', raw, re.I):
            if re.search(r"third-quarter|3q-2026|q3-2026|third quarter", href, re.I) and re.search(r"2026", href):
                links.append(abs_url(href))
    except Exception as exc:
        errors.append(f"news: {exc}")
    for link in links[:4]:
        try:
            raw, _ = fetch(link)
            blobs.append(textify(raw))
        except Exception as exc:
            errors.append(f"link: {exc}")
    try:
        raw, _ = fetch(cfg["sources"]["sec"])
        data = json.loads(raw)
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        for form, date, acc, doc in zip(forms, dates, accessions, docs):
            if date >= "2026-10-20" and form in {"8-K", "EX-99.1"}:
                acc_nodash = acc.replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/1035267/{acc_nodash}/{doc}"
                try:
                    body, _ = fetch(url)
                    blobs.append(textify(body))
                except Exception as exc:
                    errors.append(f"secfile: {exc}")
                break
    except Exception as exc:
        errors.append(f"sec: {exc}")
    return "\n".join(blobs), "; ".join(errors)


def parse_release(text):
    third = bool(re.search(r"third quarter (?:ended|of) (?:September|Sep\.?) 30,? 2026", text, re.I))
    embargo = bool(re.search(r"embargoed until", text, re.I))
    us = first(r"(?:in the )?(?:U\.S\.|United States),? da Vinci procedure(?:s| growth)?(?: grew| increased| was)?(?: approximately)?\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    if us is None:
        us = first(r"U\.S\. da Vinci procedure growth (?:was|of) approximately\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    ous = first(r"(?:outside the U\.S\.|OUS|international) da Vinci procedures?(?: grew| increased| was)?(?: approximately)?\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    dv = first(r"da Vinci procedures grew approximately\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    ion = first(r"Ion procedures grew approximately\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    placed = first(r"placed\s*([0-9,]+)\s*da Vinci surgical systems", text)
    dv5 = first(r"included\s*([0-9,]+)\s*da Vinci 5", text)
    ion_placed = first(r"placed\s*([0-9,]+)\s*Ion", text)
    trade = first(r"([0-9,]+)\s*trade-in", text)
    china = None
    china_m = re.search(r"(?:placed|placement of)\s*(?:only\s*)?([0-9]+|two|one|zero)\s*da Vinci systems? in China", text, re.I)
    if china_m:
        word = china_m.group(1).lower()
        china = {"two": 2.0, "one": 1.0, "zero": 0.0}.get(word, float(word))
    guide = re.search(r"da Vinci procedure growth of approximately\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*to\s*([0-9]+(?:\.[0-9]+)?)\s*%", text, re.I)
    guide_low = float(guide.group(1)) if guide else None
    guide_high = float(guide.group(2)) if guide else None
    closer_mid = bool(re.search(r"closer to the midpoint", text, re.I))
    closer_low = bool(re.search(r"closer to the low(?:er)? end", text, re.I))
    guide_cut = bool(re.search(r"(lowered|reduced|decreased).{0,40}procedure growth|procedure growth guidance.{0,40}(lower|below)", text, re.I))
    per_case_down = bool(re.search(r"(instrument|I&A|per procedure).{0,80}(decline|decrease|flat|lower revenue per)", text, re.I))
    per_case_limited = bool(re.search(r"(limited impact|modest impact|not material).{0,60}(per procedure|instrument)", text, re.I))
    uses_up = bool(re.search(r"(increase the number of uses|extended use|more uses)", text, re.I))
    price_number = bool(re.search(r"(2027).{0,120}(price|pricing).{0,40}[0-9]", text, re.I))
    hugo = bool(re.search(r"Hugo|Medtronic", text, re.I))
    ottava = bool(re.search(r"Ottava|Johnson & Johnson|J&J", text, re.I))
    share_loss = bool(re.search(r"(lost|losing).{0,40}(share|bid|tender)|price concession|discount", text, re.I))
    published = bool(third and (dv is not None or placed is not None) and not embargo)
    return {
        "published": published,
        "third": third,
        "us": us,
        "ous": ous,
        "dv": dv,
        "ion": ion,
        "placed": placed,
        "dv5": dv5,
        "ion_placed": ion_placed,
        "trade": trade,
        "china": china,
        "guide_low": guide_low,
        "guide_high": guide_high,
        "closer_mid": closer_mid,
        "closer_low": closer_low,
        "guide_cut": guide_cut,
        "per_case_down": per_case_down,
        "per_case_limited": per_case_limited,
        "uses_up": uses_up,
        "price_number": price_number,
        "hugo": hugo,
        "ottava": ottava,
        "share_loss": share_loss,
    }


def mark_num(v, pass_at, warn_at, label, unit="%"):
    if v is None:
        return "확인불가", f"{label} 원문 수치 없음"
    if v >= pass_at:
        return "통과", f"{label} {v:.1f}{unit}, 통과선 {pass_at:g} 이상"
    if v >= warn_at:
        return "경고", f"{label} {v:.1f}{unit}, {warn_at:g}~{pass_at:g}"
    return "이탈", f"{label} {v:.1f}{unit}, {warn_at:g} 미만"


def mark_guidance(p):
    lo, hi = p.get("guide_low"), p.get("guide_high")
    if p.get("guide_cut") or (lo is not None and lo < 13.5) or (hi is not None and hi < 15.5 and lo is not None and lo < 13.5):
        return "이탈", f"가이던스 {lo}~{hi}, 하단 하향 또는 축소 표현"
    if lo is None or hi is None:
        return "확인불가", "2026 절차 성장 구간이 원문에 없음"
    fact = f"가이둘스 {lo:.1f}~{hi:.1f}%"
    if abs(lo - 13.5) < 0.05 and abs(hi - 15.5) < 0.05 and p.get("closer_low"):
        return "경고", fact + ", 범위 유지이나 하단 근접"
    if abs(lo - 13.5) < 0.05 and abs(hi - 15.5) < 0.05:
        extra = ", 중간값 유지" if p.get("closer_mid") else ", 범위 유지"
        return "통과", fact + extra
    if lo >= 13.5 and hi >= 15.5:
        return "통과", fact + ", 하단 유지"
    return "경고", fact + ", 범위 변경"


def mark_price(p):
    if p.get("per_case_down"):
        return "이탈", "원문에 건당 기구 매출 감소 또는 정체 표현"
    if p.get("price_number") and (p.get("per_case_limited") or p.get("uses_up")):
        return "통과", "2027 가격 수치와 제한적 영향 또는 사용 횟수 확대"
    if p.get("uses_up") or p.get("per_case_limited"):
        return "경고", "방향만 확인. 건당 매출 수치 없음"
    return "확인불가", "2027 기구 가격의 건당 매출 영향이 원문에 없음"


def mark_china(p):
    if p.get("china") is None:
        return "확인불가", "중국 설치 수치 없음. 2분기 2대 부진만으로 이탈 아님"
    if p["china"] <= 2:
        return "경고", f"중국 설치 {p['china']:.0f}대, 기존 부진 반복"
    return "통과", f"중국 설치 {p['china']:.0f}대"


def mark_compete(p):
    if p.get("share_loss"):
        return "이탈", "입찰 이탈 또는 가격 양보 표현"
    if p.get("hugo") or p.get("ottava"):
        return "경고", "경쟁 언급 있음. 수주 이탈 문장은 없음"
    return "통과", "Hugo, Ottava 수주 이탈 문장 없음"


def verdict(parts):
    us, guide, price = parts["us"], parts["guide"], parts["price"]
    fails = [x for x in (us, guide, price) if x == "이탈"]
    if len(fails) >= 2:
        return "축소", "핵심 이탈 2개 이상. 비중 축소."
    if len(fails) == 1:
        return "축소 검토", "핵심 이탈 1개. 추가매수 중단, 축소 검토."
    if any(x in {"경고", "확인불가"} for x in (us, guide, price)):
        return "보유", "핵심 경고 또는 확인불가. 추가매수 보류."
    return "보유", "핵심 3개 통과. 급등 아니면 분할매수 유지."


def emit(cfg, title, lines, pri):
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title[:180],
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
        print("already scored; skip")
        save_state(st, now)
        return 0
    text, err = collect(cfg)
    parsed = parse_release(text) if text else {}
    st["parsed"] = parsed
    if not parsed.get("published"):
        if mode == "dry":
            print("not published", err)
            save_state(st, now)
            return 0
        late = now.hour > 7 or (now.hour == 7 and now.minute >= 10)
        if not text and not st.get("fail_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | ISRG | 3분기 실적",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + (err or "출처 없음") + ". 실패로 채점하지 않음.",
                "지금 할 일: IR 원문을 직접 확인. 이 실패로 매매하지 마세요.",
                "나중에: 07:10 KST 재확인. 그래도 없으면 창 종료. 만료 2026-10-21.",
                "익절/줄일 때: 조회 실패로 비중 조정 금지.",
                "하지 말 것: 조회 실패를 실적 미스로 읽기. 신용, 미수, 추격매수.",
            ]
            emit(cfg, "[ISRG] 조회 실패 | 채점 보류", lines, 4)
            st["fail_notified"] = True
            save_state(st, now)
            return 0
        if late and not st.get("expire_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | ISRG | 3분기 실적",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 07:10 KST에도 3분기 원문 없음. 2분기 수치로 채점하지 않음.",
                "지금 할 일: 보유 유지. IR을 직접 확인하고 이 알림으로 매매하지 마세요.",
                "나중에: 이 일회 창은 2026-10-21에 끝남. 다음 자동 재실행 없음.",
                "익절/줄일 때: 미발표로 비중 조정 금지.",
                "하지 말 것: 미발표를 가이던스 하향으로 읽기. 신용, 미수, 추격매수.",
            ]
            emit(cfg, "[ISRG] 원문 없음 | 보유 유지", lines, 3)
            st["expire_notified"] = True
            st["empty_notified"] = True
            save_state(st, now)
            return 0
        if st.get("empty_notified") and mode != "force":
            print("empty already sent")
            save_state(st, now)
            return 0
        lines = [
            now.strftime("%Y-%m-%d %H:%M KST") + " | ISRG | 3분기 실적",
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 3분기 원문이 아직 아님. 2분기 수치로 채점하지 않음.",
            "지금 할 일: 보유 유지. 발표 전 추가매수 금지.",
            "나중에: 07:10 KST에 원문만 다시 채점. 만료 2026-10-21.",
            "익절/줄일 때: 미발표 상태로 비중 조정 금지.",
            "하지 말 것: 2분기 15%를 3분기 실적으로 확정하기. 신용, 미수, 추격매수.",
        ]
        emit(cfg, "[ISRG] 원문 없음 | 보유 유지", lines, 3)
        st["empty_notified"] = True
        save_state(st, now)
        return 0
    us_s, us_f = mark_num(parsed.get("us"), 12, 10, "미국 da Vinci")
    ous_s, ous_f = mark_num(parsed.get("ous"), 18, 15, "해외 da Vinci")
    dv_s, dv_f = mark_num(parsed.get("dv"), 14, 13.5, "전 세계 da Vinci")
    ion_s, ion_f = mark_num(parsed.get("ion"), 30, 20, "Ion 절차")
    dv5_share = None
    if parsed.get("placed") and parsed.get("dv5") is not None and parsed["placed"]:
        dv5_share = parsed["dv5"] / parsed["placed"] * 100
    dv5_s, dv5_f = mark_num(dv5_share, 50, 40, "da Vinci 5 비중")
    trade_s, trade_f = mark_num(parsed.get("trade"), 120, 80, "트레이드인", "건")
    ch_s, ch_f = mark_china(parsed)
    g_s, g_f = mark_guidance(parsed)
    p_s, p_f = mark_price(parsed)
    c_s, c_f = mark_compete(parsed)
    action, why = verdict({"us": us_s, "guide": g_s, "price": p_s})
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | ISRG | 3분기 실적 채점",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: 결론 " + action + ". " + why
        + " 미국 " + us_s + " / " + us_f
        + ". 해외 " + ous_s + " / " + ous_f
        + ". 전 세계 " + dv_s + " / " + dv_f
        + ". Ion " + ion_s + " / " + ion_f
        + ". da Vinci 5 " + dv5_s + " / " + dv5_f
        + ". 트레이드인 " + trade_s + " / " + trade_f
        + ". 중국 " + ch_s + " / " + ch_f
        + ". 가이둘스 " + g_s + " / " + g_f
        + ". 2027 가격 " + p_s + " / " + p_f
        + ". 경쟁 " + c_s + " / " + c_f + ".",
        "지금 할 일: " + action + ". 확인불가는 합격으로 채우지 말 것.",
        "나중에: 콜에서 미국 절차, 가이둘스 문장, 2027 건당 매출만 보완. 이 알림은 2026-10-21 한 번.",
        "익절/줄일 때: 핵심 이탈이 2개면 축소. 1개면 추가매수 중단.",
        "하지 말 것: 중국 설치 부진만으로 축소. 신용, 미수, 추격매수.",
        "출처: " + cfg["sources"]["news"],
    ]
    title = f"[ISRG] {action} | 3분기 채점"
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        save_state(st, now)
        return 0
    emit(cfg, title, lines, 4 if action != "보유" else 3)
    st["final_notified"] = True
    st["score"] = action
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
