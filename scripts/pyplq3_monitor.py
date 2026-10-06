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
CFG = ROOT / "configs" / "pyplq3.json"
STATE = ROOT / "state" / "pyplq3.json"
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
        return "https://investor.pypl.com" + href
    return "https://investor.pypl.com/" + href


def collect(cfg):
    blobs, errors, links = [], [], []
    try:
        raw, _ = fetch(cfg["sources"]["news"])
        blobs.append(textify(raw))
        for href in re.findall(r'href="([^"]+)"', raw, re.I):
            if re.search(r"third-quarter|3q-26|q3-26|third quarter|earnings", href, re.I) and re.search(r"2026", href):
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
            if date >= "2026-10-27" and form in {"8-K", "EX-99.1"}:
                url = f"https://www.sec.gov/Archives/edgar/data/1633917/{acc.replace('-', '')}/{doc}"
                try:
                    body, _ = fetch(url)
                    blobs.append(textify(body))
                except Exception as exc:
                    errors.append(f"secfile: {exc}")
                break
    except Exception as exc:
        errors.append(f"sec: {exc}")
    return "\n".join(blobs), "; ".join(errors)


def growth_near(text, label):
    m = re.search(
        label + r".{0,80}?(increased|decreased|grew|declined|up|down|growth of)\s+(?:approximately\s+)?([0-9]+(?:\.[0-9]+)?)\s*%",
        text,
        re.I,
    )
    if not m:
        m = re.search(
            r"(increased|decreased|grew|declined)\s+(?:approximately\s+)?([0-9]+(?:\.[0-9]+)?)\s*%.{0,40}" + label,
            text,
            re.I,
        )
    if not m:
        return None
    sign = -1 if m.group(1).lower() in {"decreased", "declined", "down"} else 1
    return sign * float(m.group(2))


def parse_release(text):
    third = bool(re.search(r"third quarter (?:ended|of) (?:September|Sep\.?) 30,? 2026", text, re.I))
    embargo = bool(re.search(r"embargoed until", text, re.I))
    branded = growth_near(text, r"(?:online )?branded checkout(?: TPV| total payment volume)?")
    tm = growth_near(text, r"transaction margin dollars")
    eps = first(r"non-GAAP (?:diluted )?earnings per share of \$?([0-9]+\.[0-9]+)", text)
    if eps is None:
        eps = first(r"non-GAAP EPS of \$?([0-9]+\.[0-9]+)", text)
    fy_eps = first(r"(?:full[- ]year|FY'26|2026).{0,40}non-GAAP EPS.{0,20}\$?([0-9]+\.[0-9]+)", text)
    fy_tm = first(r"transaction margin dollars.{0,30}\$?([0-9]+(?:\.[0-9]+)?)\s*billion", text)
    buy_q = first(r"repurchas(?:ed|e of) \$?([0-9]+(?:\.[0-9]+)?)\s*billion", text)
    buy_fy = first(r"share repurchase.{0,30}\$?([0-9]+(?:\.[0-9]+)?)\s*billion", text)
    has_2027 = bool(re.search(r"2027", text))
    y27_down = bool(re.search(r"2027.{0,80}(decline|decrease|lower|down)", text, re.I))
    y27_up = bool(re.search(r"2027.{0,80}(increase|growth|higher|raise)", text, re.I))
    buy_cut = bool(re.search(r"(reduc|lower|cut).{0,40}(share repurchase|buyback)|share repurchase.{0,40}(below|less than) \$6", text, re.I))
    published = bool(third and (eps is not None or tm is not None or branded is not None) and not embargo)
    return {
        "published": published,
        "branded": branded,
        "tm": tm,
        "eps": eps,
        "fy_eps": fy_eps,
        "fy_tm": fy_tm,
        "buy_q": buy_q,
        "buy_fy": buy_fy,
        "has_2027": has_2027,
        "y27_down": y27_down,
        "y27_up": y27_up,
        "buy_cut": buy_cut,
    }


def band(value, beat, inline_lo, inline_hi, label, unit="%"):
    if value is None:
        return "확인불가", f"{label} 원문 수치 없음"
    if value >= beat:
        return "상회", f"{label} {value:.2f}{unit}, 상회선 {beat:g}"
    if inline_lo <= value <= inline_hi:
        return "부합", f"{label} {value:.2f}{unit}, 부합 {inline_lo:g}~{inline_hi:g}"
    return "미달", f"{label} {value:.2f}{unit}, 부합 하단 {inline_lo:g} 미만"


def mark_eps(p):
    v = p.get("eps")
    if v is None:
        return "확인불가", "non-GAAP EPS 원문 수치 없음"
    if v >= 1.34:
        return "상회", f"non-GAAP EPS {v:.2f}, 기준 1.34 이상"
    if 1.30 <= v <= 1.33:
        return "부합", f"non-GAAP EPS {v:.2f}, 한 자릿수 초반 감소"
    return "미달", f"non-GAAP EPS {v:.2f}, 1.29 이하"


def mark_2027(p):
    if not p.get("has_2027"):
        return "확인불가", "2027 가이던스 없음. 미달로 치지 않음"
    if p.get("y27_down"):
        return "미달", "2027 감소 또는 하향 표현"
    if p.get("y27_up"):
        return "상회", "2027 증가 표현"
    return "부합", "2027 언급은 있으나 방향 문장 약함"


def mark_buy(p):
    if p.get("buy_cut"):
        return "미달", "자사주 연간 가이드 축소 표현"
    q, fy = p.get("buy_q"), p.get("buy_fy")
    if q is None and fy is None:
        return "확인불가", "자사주 금액 원문 없음"
    if fy is not None and fy < 6:
        return "미달", f"연간 자사주 {fy:.1f}십억달러, 6 미만"
    if q is not None and q < 1.3:
        return "미달", f"분기 자사주 {q:.2f}십억달러, 1.3 미만"
    if q is not None and q >= 1.6 and (fy is None or fy >= 6):
        return "상회", f"분기 자사주 {q:.2f}십억달러, 연간 유지"
    return "부합", f"자사주 분기 {q}, 연간 {fy}. 6십억달러 유지로 읽음"


def verdict(parts):
    core = [parts["branded"], parts["tm"], parts["eps"]]
    misses = [x for x in core if x == "미달"]
    beats = [x for x in core if x == "상회"]
    if len(misses) >= 2 or parts["buy"] == "미달":
        return "축소", "핵심 미달 2개 이상 또는 자사주 하향. 비중 축소."
    if len(misses) == 1:
        return "추가 중단", "핵심 미달 1개. 추가 중단, 반등 시 일부 축소."
    if len(beats) >= 2 and parts["buy"] != "미달":
        return "소폭 추가", "핵심 2개 이상 상회, 미달 없음. 신규는 작게."
    return "보유", "핵심이 부합이거나 확인불가. 추가 없음."


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
        late = now.hour > 22 or (now.hour == 22 and now.minute >= 40)
        if not text and not st.get("fail_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | PYPL | 3분기 실적",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + (err or "출처 없음") + ". 실패로 채점하지 않음.",
                "지금 할 일: IR 원문을 직접 확인. 이 실패로 매매하지 마세요.",
                "나중에: 21:40 KST와 22:40 KST에 다시 봄. 만료 2026-10-27.",
                "익절/줄일 때: 조회 실패로 비중 조정 금지.",
                "하지 말 것: 조회 실패를 실적 미스로 읽기. 신용, 미수, 추격매수.",
            ]
            emit(cfg, "[PYPL] 조회 실패 | 채점 보류", lines, 4)
            st["fail_notified"] = True
            save_state(st, now)
            return 0
        if late and not st.get("expire_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | PYPL | 3분기 실적",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 22:40 KST에도 3분기 원문 없음. 2분기 수치로 채점하지 않음.",
                "지금 할 일: 보유 유지. IR을 직접 확인하고 이 알림으로 매매하지 마세요.",
                "나중에: 이 일회 창은 2026-10-27에 끝남. 다음 자동 재실행 없음.",
                "익절/줄일 때: 미발표로 비중 조정 금지.",
                "하지 말 것: 미발표를 가이던스 하향으로 읽기. 신용, 미수, 추격매수.",
            ]
            emit(cfg, "[PYPL] 원문 없음 | 보유 유지", lines, 3)
            st["expire_notified"] = True
            st["empty_notified"] = True
            save_state(st, now)
            return 0
        if st.get("empty_notified") and mode != "force":
            print("empty already sent")
            save_state(st, now)
            return 0
        lines = [
            now.strftime("%Y-%m-%d %H:%M KST") + " | PYPL | 3분기 실적",
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 3분기 원문이 아직 아님. 2분기 수치로 채점하지 않음.",
            "지금 할 일: 보유 유지. 발표 전 추가매수 금지.",
            "나중에: 21:40 KST에 원문만 다시 채점. 만료 2026-10-27.",
            "익절/줄일 때: 미발표 상태로 비중 조정 금지.",
            "하지 말 것: 2분기 브랜드 2%를 3분기 실적으로 확정하기. 신용, 미수, 추격매수.",
        ]
        emit(cfg, "[PYPL] 원문 없음 | 보유 유지", lines, 3)
        st["empty_notified"] = True
        save_state(st, now)
        return 0
    b_s, b_f = band(parsed.get("branded"), 4, 1, 3, "브랜드 결제액")
    t_s, t_f = band(parsed.get("tm"), 3, 0, 2, "거래마진 달러")
    e_s, e_f = mark_eps(parsed)
    y_s, y_f = mark_2027(parsed)
    u_s, u_f = mark_buy(parsed)
    action, why = verdict({"branded": b_s, "tm": t_s, "eps": e_s, "buy": u_s})
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | PYPL | 3분기 실적 채점",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: 결론 " + action + ". " + why
        + " 브랜드 " + b_s + " / " + b_f
        + ". 거래마진 " + t_s + " / " + t_f
        + ". EPS " + e_s + " / " + e_f
        + ". 2027 " + y_s + " / " + y_f
        + ". 자사주 " + u_s + " / " + u_f + ".",
        "지금 할 일: " + action + ". 2027이 없으면 미달로 채우지 말 것.",
        "나중에: 콜에서 브랜드 환율중립, 연간 EPS 5.38, 자사주 60억달러만 보완. 이 알림은 2026-10-27 한 번.",
        "익절/줄일 때: 핵심 미달 2개 또는 자사주 하향이면 축소. 1개면 추가 중단.",
        "하지 말 것: 인수 루머로 비중 변경. 신용, 미수, 추격매수.",
        "출처: " + cfg["sources"]["news"],
    ]
    title = f"[PYPL] {action} | 3분기 채점"
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
