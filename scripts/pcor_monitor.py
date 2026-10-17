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
CFG = ROOT / "configs" / "pcor.json"
STATE = ROOT / "state" / "pcor.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch(url):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html):
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t))


def load_state():
    st = {"final_notified": False, "empty_notified": False, "fail_notified": False, "last_key": None, "score": None}
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


def price(cfg):
    try:
        raw = json.loads(fetch(cfg["sources"]["quote"]))
        meta = raw["chart"]["result"][0]["meta"]
        return float(meta.get("regularMarketPrice"))
    except Exception:
        return None


def collect(cfg):
    blobs, errors = [], []
    for key in ("press", "ir"):
        try:
            raw = fetch(cfg["sources"][key])
            blobs.append(textify(raw))
            for href in re.findall(r'href="([^"]+)"', raw, re.I):
                if re.search(r"third-quarter-2026|q3-2026|third quarter 2026", href, re.I):
                    link = href if href.startswith("http") else "https://www.procore.com" + href
                    try:
                        blobs.append(textify(fetch(link)))
                    except Exception as exc:
                        errors.append(str(exc))
        except Exception as exc:
            errors.append(f"{key}: {exc}")
    try:
        sec = json.loads(fetch(cfg["sources"]["sec"]))
        recent = sec.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        docs = recent.get("primaryDocument", [])
        acc = recent.get("accessionNumber", [])
        for i, form in enumerate(forms[:12]):
            if form == "8-K" and dates[i] >= "2026-11-04":
                cik = str(int(cfg["cik"]))
                accn = acc[i].replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{docs[i]}"
                blobs.append(textify(fetch(url)))
    except Exception as exc:
        errors.append(f"sec: {exc}")
    return "\n".join(blobs), "; ".join(errors)


def parse_release(text, cfg):
    third = bool(re.search(r"third quarter (?:ended|of) (?:September|Sep\.?) 30,? 2026", text, re.I))
    revenue = first(r"revenue (?:was|of) \$?\s*([0-9]{3}(?:\.[0-9]+)?)\s*million", text)
    yoy = first(r"increase of ([0-9]+(?:\.[0-9]+)?)\s*(?:%|percent) year-over-year", text)
    if yoy is None and revenue:
        yoy = (revenue / cfg["baseline"]["q3_base_m"] - 1) * 100
    om = first(r"non-GAAP operating margin (?:was|of) ([0-9]+(?:\.[0-9]+)?)\s*(?:%|percent)", text)
    fcf = first(r"free cash (?:in)?flow(?: for the third quarter)? (?:was|of) \$?\s*([0-9]+(?:\.[0-9]+)?)\s*million", text)
    cust = first(r"([0-9,]{3,})\s*(?:organic )?customers contributing more than \$?100,000", text)
    cust_yoy = first(r"100,000 of annual recurring revenue totaled [0-9,]+\s*.{0,40}?increase of ([0-9]+(?:\.[0-9]+)?)\s*(?:%|percent)", text)
    grr = first(r"gross revenue retention rate of ([0-9]+(?:\.[0-9]+)?)\s*(?:%|percent)", text)
    g27 = first(r"(?:full year|fiscal year) 2027.{0,180}?([0-9](?:,[0-9]{3})+(?:\.[0-9]+)?)\s*million", text)
    raised = bool(re.search(r"2027.{0,80}(rais|increas|above)", text, re.I))
    cut = bool(re.search(r"2027.{0,80}(lower|reduc|cut|below prior)", text, re.I))
    kept = bool(re.search(r"2027.{0,80}(maintain|reaffirm|unchanged)", text, re.I))
    published = bool(third and revenue is not None)
    acq = parse_acquisition(text)
    return {
        **acq,
        "published": published,
        "revenue": revenue,
        "yoy": yoy,
        "om": om,
        "fcf": fcf,
        "cust": cust,
        "cust_yoy": cust_yoy,
        "grr": grr,
        "g27": g27,
        "raised": raised,
        "cut": cut,
        "kept": kept,
    }


NUM = r"([0-9]+(?:\.[0-9]+)?)"
BOILER = re.compile(r"forward-looking|risks and uncertainties|safe harbor|undue reliance", re.I)


def parse_acquisition(text):
    """DroneDeploy contribution / organic growth and a data center sentence with a number (reference only)."""
    dd_rev = first(r"DroneDeploy[^.]{0,80}?(?:contributed|contribution of|revenue of|added)\s*(?:approximately\s*)?\$\s*" + NUM + r"\s*million", text)
    if dd_rev is None:
        dd_rev = first(r"\$\s*" + NUM + r"\s*million[^.]{0,60}?(?:from|attributable to|contributed by)\s*(?:the\s*)?DroneDeploy", text)
    organic_yoy = first(r"organic revenue (?:growth|grew|increased)[^.]{0,40}?" + NUM + r"\s*(?:%|percent)", text)
    if organic_yoy is None:
        organic_yoy = first(r"excluding (?:the (?:impact|contribution) of )?(?:the )?DroneDeploy[^.]{0,80}?(?:revenue )?(?:grew|growth of|increased|increase of)\s*" + NUM + r"\s*(?:%|percent)", text)
    g27_organic = first(r"2027[^.]{0,160}?(?:organic|excluding (?:the (?:impact|contribution) of )?(?:the )?DroneDeploy)[^.]{0,100}?" + NUM + r"\s*(?:%|percent)", text)
    dc_sentence = None
    for sent in re.split(r"(?<=[.!?])\s+", text or ""):
        if len(sent) > 400 or BOILER.search(sent):
            continue
        if re.search(r"data cent(?:er|re)s?", sent, re.I) and re.search(r"\$\s?[0-9]|[0-9]+(?:\.[0-9]+)?\s*(?:%|percent)", sent):
            dc_sentence = sent.strip()[:220].rstrip(".")
            break
    return {
        "dd_mentioned": bool(re.search(r"DroneDeploy", text or "")),
        "dd_rev": dd_rev,
        "organic_yoy": organic_yoy,
        "g27_organic": g27_organic,
        "dc_sentence": dc_sentence,
    }


def acquisition_in_period(cfg, period_end="2026-09-30"):
    acq = cfg.get("acquisition") or {}
    return bool(acq.get("closed")) and str(acq["closed"]) <= period_end


def cap_hold(mark, fact, why):
    if mark == "통과":
        return "보류", fact + f", {why}라 최대 보류"
    return mark, fact


def mark_revenue(parsed, cfg, th):
    if not acquisition_in_period(cfg):
        return band(parsed.get("yoy"), th["rev_pass"], th["rev_hold"], "%")
    if parsed.get("organic_yoy") is not None:
        s_, f_ = band(parsed["organic_yoy"], th["rev_pass"], th["rev_hold"], "%")
        return s_, f_ + " (DroneDeploy 제외 증가율)"
    if parsed.get("dd_rev") is not None and parsed.get("revenue") is not None:
        org = ((parsed["revenue"] - parsed["dd_rev"]) / cfg["baseline"]["q3_base_m"] - 1) * 100
        s_, f_ = band(org, th["rev_pass"], th["rev_hold"], "%")
        return s_, f_ + f" (DroneDeploy {parsed['dd_rev']:g}백만달러 빼고 계산)"
    s_, f_ = band(parsed.get("yoy"), th["rev_pass"], th["rev_hold"], "%")
    return cap_hold(s_, f_ + " (인수 효과 포함)", "DroneDeploy 기여분 미공시")


def reference_line(parsed, cfg):
    acq = cfg.get("acquisition") or {}
    parts = []
    if acq:
        if parsed.get("dd_rev") is not None:
            parts.append(f"DroneDeploy 기여 매출 {parsed['dd_rev']:g}백만달러")
        elif parsed.get("organic_yoy") is not None:
            parts.append(f"DroneDeploy 기여 금액 미공시, 제외 증가율 {parsed['organic_yoy']:g}% 공시")
        else:
            parts.append("DroneDeploy 기여분 미공시")
        parts.append(f"인수 완료 {acq.get('closed')}, 약 {acq.get('price_m')}백만달러 현금")
    if parsed.get("dc_sentence"):
        parts.append("데이터센터 원문: " + parsed["dc_sentence"])
    return "참고(판정 미반영): " + ". ".join(parts) + "." if parts else None


def band(v, pass_at, hold_at, unit):
    if v is None:
        return "확인 불가", "원문 수치 없음"
    if v >= pass_at:
        return "통과", f"사실 {v:.1f}{unit}, 통과선 {pass_at:g}{unit} 이상"
    if v >= hold_at:
        return "보류", f"사실 {v:.1f}{unit}, 보류 {hold_at:g}~{pass_at:g}{unit}"
    return "축소", f"사실 {v:.1f}{unit}, 축소선 {hold_at:g}{unit} 미만"


def mark_guide(parsed, th, cfg=None):
    cfg = cfg or {}
    acq = acquisition_in_period(cfg)
    if parsed.get("cut"):
        return "축소", "사실 2027 가이던스 하향 표현"
    if acq and parsed.get("g27_organic") is not None:
        s_, f_ = band(parsed["g27_organic"], th["g27_pass"], th["g27_hold"], "%")
        return s_, f_ + " (DroneDeploy 제외 2027 증가율)"
    base = (cfg.get("baseline") or {}).get("fy26_guide_high_m", 1514)
    if parsed.get("raised"):
        s_, f_ = "통과", "사실 2027 가이던스 상향 표현"
    elif parsed.get("g27"):
        growth = (parsed["g27"] / base - 1) * 100
        s_, f_ = band(growth, th["g27_pass"], th["g27_hold"], "%")
        f_ += f" (인수 전 2026 가이던스 {base:g} 대비)"
    elif parsed.get("kept"):
        return "보류", "사실 2027 가이던스 유지 표현, 금액 없음"
    else:
        return "확인 불가", "원문에 2027 가이던스 금액과 상하향 표현 없음"
    if acq:
        return cap_hold(s_, f_ + " (인수 매출 포함 가능)", "DroneDeploy 제외 수치 미공시")
    return s_, f_


def overall(marks):
    rev, om, fcf, cust, grr, guide = marks
    if grr == "축소" or guide == "축소" or sum(x == "축소" for x in marks) >= 2:
        return "축소"
    if rev == "통과" and om == "통과" and "축소" not in marks:
        return "통과"
    return "보류"


def action(score, px):
    px_txt = f"{px:.2f}달러" if px is not None else "가격 확인 불가"
    if score == "통과":
        return (
            f"1차만 분할. 현재 {px_txt}. 50달러 근처 1차, 45달러 이하는 2차.",
            "20% 마진과 15% 증가가 같이 깨지면 2차도 중단.",
            "실적 직후 추격매수, 신용, 미수.",
        )
    if score == "축소":
        return (
            f"추가매수 중단. 현재 {px_txt}. 45달러 아래도 가이던스 확인 전 보류.",
            "유지율 93% 미만 또는 2027 하향이면 비중 축소 검토.",
            "축소를 즉시 전량 매도로 읽기.",
        )
    return (
        f"추가매수 보류. 현재 {px_txt}. 기존 보유만 유지.",
        "통과 2개가 늘기 전 1차 분할 금지.",
        "확인 불가를 통과로 채우기.",
    )


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
    th = cfg["thresholds"]
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
    if st.get("final_notified") and mode not in {"force", "dry"}:
        print("already scored; skip")
        save_state(st, now)
        return 0
    px = price(cfg)
    text, err = collect(cfg)
    parsed = parse_release(text, cfg) if text else {}
    st["parsed"] = parsed
    st["price"] = px
    if not parsed.get("published"):
        if mode == "dry":
            print("DRY RUN: not published", err)
            return 0
        if not text and not st.get("fail_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | PCOR | 3분기 실적",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + (err or "출처 없음") + ". 실패로 채점하지 않음.",
                "지금 할 일: IR 원문을 직접 확인. 이 실패로 매매하지 마세요.",
                "나중에: 18:00 KST 재확인. 만료 2026-11-06.",
                "익절/줄일 때: 조회 실패로 비중 조정 금지.",
                "하지 말 것: 조회 실패를 실적 미스로 읽기.",
            ]
            emit(cfg, "[PCOR] 조회 실패 | 채점 보류", lines, 4)
            st["fail_notified"] = True
            save_state(st, now)
            return 0
        if st.get("empty_notified") and mode != "force":
            print("empty already sent")
            save_state(st, now)
            return 0
        lines = [
            now.strftime("%Y-%m-%d %H:%M KST") + " | PCOR | 3분기 실적",
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 3분기 원문이 아직 아님. 2분기 수치로 채점하지 않음. 가격 " + (f"{px:.2f}달러" if px else "확인 불가") + ".",
            "지금 할 일: 추가매수 보류. 50달러 1차, 45달러 2차는 채점 통과 전 금지.",
            "나중에: 18:00 KST에 원문만 다시 채점. 만료 2026-11-06.",
            "익절/줄일 때: 미발표 상태로 비중 조정 금지.",
            "하지 말 것: 가이던스 상단을 실적으로 확정하기.",
        ]
        emit(cfg, "[PCOR] 원문 없음 | 추가매수 보류", lines, 3)
        st["empty_notified"] = True
        save_state(st, now)
        return 0
    rev_s, rev_f = mark_revenue(parsed, cfg, th)
    om_s, om_f = band(parsed.get("om"), th["om_pass"], th["om_hold"], "%")
    fcf_s, fcf_f = band(parsed.get("fcf"), th["fcf_pass"], th["fcf_hold"], "백만달러")
    cust_s, cust_f = band(parsed.get("cust_yoy"), th["cust_pass"], th["cust_hold"], "%")
    grr_s, grr_f = band(parsed.get("grr"), th["grr_pass"], th["grr_hold"], "%")
    g_s, g_f = mark_guide(parsed, th, cfg)
    score = overall((rev_s, om_s, fcf_s, cust_s, grr_s, g_s))
    do, trim, dont = action(score, px)
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | PCOR | 3분기 실적 채점",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: 종합 " + score
        + ". 매출 증가율 " + rev_s + " / " + rev_f
        + ". non-GAAP 영업이익률 " + om_s + " / " + om_f
        + ". 자유현금흐름 " + fcf_s + " / " + fcf_f
        + ". 10만 달러 고객 " + cust_s + " / " + cust_f
        + ". 총매출 유지율 " + grr_s + " / " + grr_f
        + ". 2027 가이던스 " + g_s + " / " + g_f + ".",
        "지금 할 일: " + do,
        "나중에: 확인 불가 항목은 콜 원문에서만 보완. 이 알림은 2026-11-06 한 번.",
        "익절/줄일 때: " + trim,
        "하지 말 것: " + dont,
        "출처: " + cfg["sources"]["press"],
    ]
    ref = reference_line(parsed, cfg)
    if ref:
        lines.insert(len(lines) - 1, ref)
    title = f"[PCOR] {score} | {do.split('.')[0]}"
    if mode == "dry":
        print("DRY RUN: no ntfy, no state write")
        print(json.dumps({k: parsed.get(k) for k in ("revenue", "yoy", "organic_yoy", "dd_rev", "g27", "g27_organic", "dd_mentioned", "dc_sentence")}, ensure_ascii=False))
        print(title)
        print("\n".join(lines))
        return 0
    emit(cfg, title, lines, 4 if score != "보류" else 3)
    st["final_notified"] = True
    st["score"] = score
    st["last_key"] = json.dumps(parsed, sort_keys=True, default=str)
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
