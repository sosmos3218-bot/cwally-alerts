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
CFG = ROOT / "configs" / "tsmc1015.json"
STATE = ROOT / "state" / "tsmc1015.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


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


def nums(pattern, text):
    return [float(x.replace(",", "")) for x in re.findall(pattern, text, re.I)]


def first(pattern, text):
    m = re.search(pattern, text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def band(text, quarter):
    m = re.search(
        quarter + r".{0,500}?between US\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion and US\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion",
        text,
        re.I,
    )
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def parse_release(text):
    third = bool(re.search(r"third quarter (?:ended|of) (?:September|Sep\.?) 30,? 2026", text, re.I))
    embargo = bool(re.search(r"embargoed until", text, re.I))
    revenue = first(
        r"(?:third quarter revenue (?:was|of)|revenue was|in US dollar terms, third quarter revenue was) US\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion",
        text,
    )
    if revenue is None:
        hits = nums(r"US\$\s*([0-9]{2}(?:\.[0-9]+)?)\s*billion", text)
        hits = [x for x in hits if 40 <= x <= 70]
        revenue = hits[0] if hits else None
    gm = first(r"gross margin (?:was|of|reached|came in at)\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    om = first(r"operating margin (?:was|of|reached|came in at)\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    q4_low, q4_high = band(text, r"fourth quarter 2026")
    if q4_low is None:
        q4_low, q4_high = band(text, r"fourth quarter")
    q4_gm = re.search(
        r"fourth quarter.{0,400}?gross (?:profit )?margin is expected to be between ([0-9]+(?:\.[0-9]+)?)% and ([0-9]+(?:\.[0-9]+)?)%",
        text,
        re.I,
    )
    capex_2027 = nums(r"2027 capital (?:budget|expenditure).{0,40}?US\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion", text)
    price_up = bool(re.search(r"(price increase|raise (?:our )?prices|pricing (?:will|to) increase|higher (?:wafer )?prices)", text, re.I))
    price_down = bool(re.search(r"(price concession|discount|lower (?:wafer )?prices|pricing pressure)", text, re.I))
    cowos = bool(re.search(r"CoWoS|advanced packaging", text, re.I))
    cowos_tight = bool(re.search(r"(CoWoS|advanced packaging).{0,80}(tight|constrained|shortage|insufficient)", text, re.I))
    n2 = bool(re.search(r"\bN2\b|2nm|2-nanometer|2 nanometer", text, re.I))
    n2_mix = first(r"(?:N2|2nm|2-nanometer).{0,40}?([0-9]+(?:\.[0-9]+)?)\s*% of wafer revenue", text)
    published = bool(third and revenue is not None and gm is not None and not embargo)
    return {
        "published": published,
        "third": third,
        "embargo": embargo,
        "revenue": revenue,
        "gm": gm,
        "om": om,
        "q4_low": q4_low,
        "q4_high": q4_high,
        "q4_gm_low": float(q4_gm.group(1)) if q4_gm else None,
        "q4_gm_high": float(q4_gm.group(2)) if q4_gm else None,
        "capex_2027": capex_2027[0] if capex_2027 else None,
        "price_up": price_up,
        "price_down": price_down,
        "cowos": cowos,
        "cowos_tight": cowos_tight,
        "n2": n2,
        "n2_mix": n2_mix,
    }


def mark_rev(v):
    if v is None:
        return "확인 불가", "원문에 달러 매출 없음"
    if v > 45.8:
        return "합격", f"사실 {v:.2f}억 달러, 가이던스 상단 458억 초과"
    if v >= 44.6:
        return "보류", f"사실 {v:.2f}억 달러, 가이던스 446~458억 안"
    return "실패", f"사실 {v:.2f}억 달러, 가이던스 하단 446억 미만"


def mark_gm(v):
    if v is None:
        return "확인 불가", "원문에 매출총이익률 없음"
    if v >= 67:
        return "합격", f"사실 {v:.1f}%, 67% 이상"
    if v >= 65:
        return "보류", f"사실 {v:.1f}%, 가이던스 안이나 67% 미만"
    return "실패", f"사실 {v:.1f}%, 가이던스 하단 65% 미만"


def mark_q4(parsed):
    lo, hi = parsed.get("q4_low"), parsed.get("q4_high")
    base = parsed.get("revenue")
    if lo is None or hi is None or not base:
        return "확인 불가", "원문에 4분기 달러 가이던스 구간 없음", None
    mid = (lo + hi) / 2
    qoq = (mid / base - 1) * 100
    fact = f"사실 중간값 {mid:.2f}억 달러, 3분기 대비 {qoq:+.1f}%"
    if qoq >= 8:
        return "합격", fact, qoq
    if qoq >= 3:
        return "보류", fact, qoq
    return "실패", fact, qoq


def mark_capex_price(parsed):
    if parsed.get("price_down"):
        return "실패", "사실 원문에 가격 양보 또는 인하 표현"
    if parsed.get("capex_2027") and parsed.get("price_up"):
        return "합격", f"사실 2027 캐팩스 {parsed['capex_2027']:.0f}억 달러, 가격 인상 표현 있음"
    if parsed.get("price_up"):
        return "합격", "사실 가격 인상 표현 있음. 2027 캐팩스 금액은 원문 없음"
    if parsed.get("capex_2027"):
        return "보류", f"사실 2027 캐팩스 {parsed['capex_2027']:.0f}억 달러. 가격 코멘트 없음"
    return "확인 불가", "원문에 2027 캐팩스 금액과 가격 코멘트 없음"


def mark_capacity(parsed):
    if not parsed.get("cowos") and not parsed.get("n2"):
        return "확인 불가", "원문에 CoWoS, 2나노 언급 없음"
    bits = []
    if parsed.get("n2_mix") is not None:
        bits.append(f"2나노 웨이퍼 비중 {parsed['n2_mix']:.1f}%")
    elif parsed.get("n2"):
        bits.append("2나노 언급 있음, 비중 수치 없음")
    if parsed.get("cowos_tight"):
        bits.append("CoWoS 또는 선진 패키징 타이트")
    elif parsed.get("cowos"):
        bits.append("CoWoS 또는 선진 패키징 언급, 병목 표현 없음")
    fact = "사실 " + ", ".join(bits)
    if parsed.get("n2") and parsed.get("cowos_tight"):
        return "합격", fact
    return "보류", fact


def overall(parts):
    rev, gm, q4, cap, capc = parts
    forward_fail = sum(x == "실패" for x in (q4, cap, capc))
    if rev == "실패" or gm == "실패" or forward_fail >= 2:
        return "실패"
    if rev == "합격" and gm == "합격" and forward_fail == 0:
        return "합격"
    return "보류"


def action(score):
    if score == "합격":
        return "보유 유지. 판단 변경 없음. 조정 시 분할 매수.", "기존 보유 유지, 고점 추격 금지.", "원문 없는 법인 목표가로 확대 금지."
    if score == "실패":
        return "비중 확대 보류로 변경. 기존 보유는 2027 캐팩스 설명 전 유지.", "가이던스 하단 이탈이면 추가매수 중단.", "실패를 즉시 전량 매도로 읽지 말 것."
    return "보유 유지, 비중 확대 보류. 판단 변경 없음.", "4분기 중간값이 한 자릿수면 추가매수 보류.", "확인 불가 항목을 합격으로 채우지 말 것."


def collect(cfg):
    blobs = []
    errors = []
    for key in ("q3_page", "quarterly", "news"):
        url = cfg["sources"][key]
        try:
            raw, final = fetch(url)
            blobs.append(textify(raw))
            for href in re.findall(r'href="([^"]+)"', raw, re.I):
                if re.search(r"2026.*q3|third-quarter|3q26|q3-2026", href, re.I) and href.lower().endswith((".htm", ".html", ".pdf")):
                    link = href if href.startswith("http") else "https://investor.tsmc.com" + href
                    try:
                        raw2, _ = fetch(link)
                        blobs.append(textify(raw2) if not link.lower().endswith(".pdf") else raw2)
                    except Exception as exc:
                        errors.append(str(exc))
        except Exception as exc:
            errors.append(f"{key}: {exc}")
    return "\n".join(blobs), "; ".join(errors)


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
        if not text and not st.get("fail_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | TSMC | 3분기 실적",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + (err or "출처 없음") + ". 실패로 채점하지 않음.",
                "지금 할 일: IR 원문을 직접 확인. 이 실패로 매매하지 마세요.",
                "나중에: 17:10 KST 재확인. 그래도 없으면 창 종료. 만료 2026-10-15.",
                "익절/줄일 때: 조회 실패로 비중 조정 금지.",
                "하지 말 것: 조회 실패를 실적 미스로 읽기.",
            ]
            emit(cfg, "[TSMC] 조회 실패 | 채점 보류", lines, 4)
            st["fail_notified"] = True
            save_state(st, now)
            return 0
        if st.get("empty_notified") and mode != "force":
            print("empty already sent")
            save_state(st, now)
            return 0
        lines = [
            now.strftime("%Y-%m-%d %H:%M KST") + " | TSMC | 3분기 실적",
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 3분기 원문이 아직 아님. 2분기 수치로 채점하지 않음.",
            "지금 할 일: 보유 유지. 발표 전 추가매수 금지.",
            "나중에: 17:10 KST에 원문만 다시 채점. 만료 2026-10-15.",
            "익절/줄일 때: 미발표 상태로 비중 조정 금지.",
            "하지 말 것: 가이던스 상단을 실적으로 확정하기.",
        ]
        emit(cfg, "[TSMC] 원문 없음 | 보유 유지", lines, 3)
        st["empty_notified"] = True
        save_state(st, now)
        return 0
    rev_s, rev_f = mark_rev(parsed.get("revenue"))
    gm_s, gm_f = mark_gm(parsed.get("gm"))
    q4_s, q4_f, _ = mark_q4(parsed)
    cap_s, cap_f = mark_capex_price(parsed)
    capc_s, capc_f = mark_capacity(parsed)
    score = overall((rev_s, gm_s, q4_s, cap_s, capc_s))
    do, trim, dont = action(score)
    om = parsed.get("om")
    om_txt = f"{om:.1f}%" if om is not None else "확인 불가"
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | TSMC | 3분기 실적 채점",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: 종합 " + score
        + ". 매출 " + rev_s + " / " + rev_f
        + ". 매출총이익률 " + gm_s + " / " + gm_f
        + ". 영업이익률 사실 " + om_txt
        + ". 4분기 " + q4_s + " / " + q4_f
        + ". 2027 캐팩스·가격 " + cap_s + " / " + cap_f
        + ". CoWoS·2나노 " + capc_s + " / " + capc_f + ".",
        "지금 할 일: " + do,
        "나중에: 확인 불가 항목은 콜 트랜스크립트에서만 보완. 이 알림은 2026-10-15 한 번.",
        "익절/줄일 때: " + trim,
        "하지 말 것: " + dont + " 신용, 미수, 추격매수.",
        "출처: " + cfg["sources"]["q3_page"],
    ]
    title = f"[TSMC] {score} | {do.split('.')[0]}"
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        save_state(st, now)
        return 0
    emit(cfg, title, lines, 4 if score != "보류" else 3)
    st["final_notified"] = True
    st["score"] = score
    st["last_key"] = json.dumps(parsed, sort_keys=True, default=str)
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
