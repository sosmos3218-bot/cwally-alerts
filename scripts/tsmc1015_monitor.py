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


def gm_band(text, label):
    m = re.search(
        label + r".{0,400}?gross (?:profit )?margin is expected to be between ([0-9]+(?:\.[0-9]+)?)% and ([0-9]+(?:\.[0-9]+)?)%",
        text,
        re.I,
    )
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def capex_near(text, year):
    hits = nums(year + r".{0,80}?capital (?:budget|expenditure).{0,40}?US\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion", text)
    if hits:
        return hits[0]
    hits = nums(r"capital (?:budget|expenditure).{0,40}?(?:for )?" + year + r".{0,40}?US\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion", text)
    return hits[0] if hits else None


def parse_release(text):
    third = bool(re.search(r"third quarter (?:ended|of) (?:September|Sep\.?) 30,? 2026", text, re.I))
    embargo = bool(re.search(r"embargoed until", text, re.I))
    revenue = first(
        r"(?:third quarter revenue (?:was|of)|in US dollar terms, third quarter revenue was) US\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion",
        text,
    )
    if revenue is None:
        hits = [x for x in nums(r"US\$\s*([0-9]{2}(?:\.[0-9]+)?)\s*billion", text) if 40 <= x <= 70]
        revenue = hits[0] if hits else None
    gm = first(r"gross margin (?:was|of|reached|came in at)\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    om = first(r"operating margin (?:was|of|reached|came in at)\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    q4_low, q4_high = band(text, r"fourth quarter 2026")
    if q4_low is None:
        q4_low, q4_high = band(text, r"fourth quarter")
    q4_gm_low, q4_gm_high = gm_band(text, r"fourth quarter")
    fy_gm_low, fy_gm_high = gm_band(text, r"full year|2026")
    capex_2026 = capex_near(text, "2026")
    capex_2027 = capex_near(text, "2027")
    price_up = bool(re.search(r"(price increase|raise (?:our )?prices|pricing (?:will|to) increase|higher (?:wafer )?prices|wafer price.{0,20}increase)", text, re.I))
    price_down = bool(re.search(r"(price concession|discount|lower (?:wafer )?prices|pricing pressure)", text, re.I))
    asp = first(r"(?:wafer )?(?:price|ASP).{0,30}?([0-9]+(?:\.[0-9]+)?)\s*%", text)
    cautious = bool(re.search(r"2027.{0,80}(slow|softer|moderate|pause|cautious|digest)", text, re.I))
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
        "q4_gm_low": q4_gm_low,
        "q4_gm_high": q4_gm_high,
        "fy_gm_low": fy_gm_low,
        "fy_gm_high": fy_gm_high,
        "capex_2026": capex_2026,
        "capex_2027": capex_2027,
        "price_up": price_up,
        "price_down": price_down,
        "asp": asp,
        "cautious": cautious,
    }


def fmt(v, suffix=""):
    if v is None:
        return "확인 불가"
    if isinstance(v, float):
        return f"{v:.2f}{suffix}" if v < 100 else f"{v:.0f}{suffix}"
    return str(v)


def judge(parsed, cfg):
    rules = cfg["rules"]
    floor = rules["gm_floor"]
    gm_guide = parsed.get("q4_gm_low")
    if gm_guide is None:
        gm_guide = parsed.get("fy_gm_low")
    capex_up = False
    if parsed.get("capex_2027") and parsed["capex_2027"] > rules["capex_2026_high"]:
        capex_up = True
    if parsed.get("capex_2026") and parsed["capex_2026"] > rules["capex_2026_high"]:
        capex_up = True
    missing = []
    if parsed.get("revenue") is None:
        missing.append("달러 매출")
    if parsed.get("gm") is None:
        missing.append("매출총이익률")
    if parsed.get("q4_low") is None:
        missing.append("4분기 가이던스")
    if parsed.get("capex_2026") is None and parsed.get("capex_2027") is None:
        missing.append("CAPEX")
    if not parsed.get("price_up") and not parsed.get("price_down") and parsed.get("asp") is None:
        missing.append("웨이퍼 ASP")
    if parsed.get("price_down") or parsed.get("cautious") or (gm_guide is not None and gm_guide < floor):
        why = []
        if gm_guide is not None and gm_guide < floor:
            why.append(f"가이던스 매출총이익률 하단 {gm_guide:.1f}% < {floor:.0f}%")
        if parsed.get("price_down"):
            why.append("가격 인하 또는 양보")
        if parsed.get("cautious"):
            why.append("2027 수요 둔화 언급")
        return "하향", "; ".join(why)
    if capex_up and parsed.get("price_up") and gm_guide is not None and gm_guide >= floor and not missing:
        return "상향", f"CAPEX 상향, 마진 하단 {gm_guide:.1f}%, 웨이퍼 가격 인상 언급"
    note = "조건이 섞이거나 항목이 빠져 472달러 중립 유지"
    if missing:
        note += ". 확인 불가: " + ", ".join(missing)
    return "유지", note


def action(score):
    if score == "상향":
        return "중립을 상향으로 갱신. 추가 매수는 분할만.", "마진 하단이 다시 60% 아래로 내려가면 상향 철회.", "가격 급등을 추격하지 말 것."
    if score == "하향":
        return "중립을 하향으로 갱신. 추가매수 중단.", "마진 하단 60% 회복과 가격 인상 확인 전 비중 확대 금지.", "하향을 즉시 전량 매도로 읽지 말 것."
    return "472달러 중립 유지. 비중 변경 없음.", "빠진 항목이 콜 원문에 있으면 그때만 재판단.", "확인 불가를 상향으로 채우지 말 것."


def collect(cfg):
    blobs = []
    errors = []
    for key in ("q3_page", "quarterly", "news"):
        url = cfg["sources"][key]
        try:
            raw, _ = fetch(url)
            blobs.append(textify(raw))
            for href in re.findall(r'href="([^"]+)"', raw, re.I):
                if re.search(r"2026.*q3|third-quarter|3q26|q3-2026|transcript|conference", href, re.I) and href.lower().endswith((".htm", ".html", ".pdf")):
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


def field_line(parsed):
    q4 = "확인 불가"
    if parsed.get("q4_low") is not None:
        q4 = f"{parsed['q4_low']:.2f}~{parsed['q4_high']:.2f}십억달러"
        if parsed.get("q4_gm_low") is not None:
            q4 += f", 매출총이익률 {parsed['q4_gm_low']:.1f}~{parsed['q4_gm_high']:.1f}%"
    asp = "인상 언급" if parsed.get("price_up") else ("인하 언급" if parsed.get("price_down") else "확인 불가")
    if parsed.get("asp") is not None:
        asp += f", 수치 {parsed['asp']:.1f}%"
    return (
        f"달러 매출 {fmt(parsed.get('revenue'), '십억달러')}"
        f", 매출총이익률 {fmt(parsed.get('gm'), '%')}"
        f", 4분기 {q4}"
        f", 2026 CAPEX {fmt(parsed.get('capex_2026'), '십억달러')}"
        f", 2027 CAPEX {fmt(parsed.get('capex_2027'), '십억달러')}"
        f", 웨이퍼 ASP {asp}"
    )


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if mode == "install":
        lines = [
            now.strftime("%Y-%m-%d %H:%M KST") + " | TSMC | 설치 확인",
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 설치 확인. 매매 신호 아님. 기준은 2026-10-08 472달러 중립.",
            "지금 할 일: 없음. 10월 15일 16:30 KST에 보도자료와 콜만 보고 판정.",
            "나중에: 원문이 없으면 17:10에 한 번 재확인. 가격 반응은 넣지 않음.",
            "익절/줄일 때: 이 확인 알림으로 비중 조정 금지.",
            "하지 말 것: 설치 확인을 상향이나 하향으로 읽기.",
        ]
        emit(cfg, "[TSMC] 설치 확인 | 매매 신호 아님", lines, 3)
        print("install sent")
        return 0
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
                now.strftime("%Y-%m-%d %H:%M KST") + " | TSMC | 3분기 판정",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + (err or "출처 없음") + ". 실패로 하향하지 않음. 중립 유지.",
                "지금 할 일: IR 원문을 직접 확인. 이 실패로 매매하지 마세요.",
                "나중에: 17:10 KST 재확인. 그래도 없으면 창 종료.",
                "익절/줄일 때: 조회 실패로 비중 조정 금지.",
                "하지 말 것: 조회 실패를 실적 미스로 읽기.",
            ]
            emit(cfg, "[TSMC] 조회 실패 | 중립 유지", lines, 4)
            st["fail_notified"] = True
            save_state(st, now)
            return 0
        if st.get("empty_notified") and mode != "force":
            print("empty already sent")
            save_state(st, now)
            return 0
        lines = [
            now.strftime("%Y-%m-%d %H:%M KST") + " | TSMC | 3분기 판정",
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 3분기 원문이 아직 아님. 472달러 중립 유지. 2분기 수치로 채점하지 않음.",
            "지금 할 일: 보유 유지. 발표 전 추가매수 금지.",
            "나중에: 17:10 KST에 원문만 다시 판정.",
            "익절/줄일 때: 미발표 상태로 비중 조정 금지.",
            "하지 말 것: 월매출 합산을 공식 달러 매출로 확정하기.",
        ]
        emit(cfg, "[TSMC] 원문 없음 | 중립 유지", lines, 3)
        st["empty_notified"] = True
        save_state(st, now)
        return 0
    score, why = judge(parsed, cfg)
    do, later, dont = action(score)
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | TSMC | 3분기 가이던스 판정",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + score + ". " + why + ". " + field_line(parsed) + ". 가격 반응은 사용하지 않음.",
        "지금 할 일: " + do,
        "나중에: " + later,
        "익절/줄일 때: " + dont,
        "하지 말 것: 미국 프리마켓 가격으로 이 판정을 바꾸기. 투자 권유 아님.",
        "출처: " + cfg["sources"]["q3_page"],
    ]
    title = f"[TSMC] {score} | {do.split('.')[0]}"
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        save_state(st, now)
        return 0
    emit(cfg, title, lines, 4 if score != "유지" else 3)
    st["final_notified"] = True
    st["score"] = score
    st["last_key"] = json.dumps(parsed, sort_keys=True, default=str)
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
