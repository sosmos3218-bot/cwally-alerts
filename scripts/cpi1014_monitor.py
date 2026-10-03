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
CFG = ROOT / "configs" / "cpi1014.json"
STATE = ROOT / "state" / "cpi1014.json"
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


def signed(verb, value):
    if value is None:
        return None
    if re.search(r"unchanged|little change|essentially unchanged", verb or "", re.I):
        return 0.0
    if re.search(r"decreas|fell|declin|edged down|down", verb or "", re.I):
        return -abs(value)
    return value


def grab(text, pattern):
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    verb = m.group(1) if m.lastindex and m.lastindex >= 2 else ""
    raw = m.group(m.lastindex)
    return signed(verb, num(raw))


def parse_bls(text):
    september = bool(re.search(r"CONSUMER PRICE INDEX\s*-\s*SEPTEMBER 2026", text, re.I))
    embargo = bool(re.search(r"EMBARGOED UNTIL", text, re.I) and re.search(r"October 14, 2026", text))
    headline_mom = grab(
        text,
        r"CPI-U\)? (increased|rose|decreased|fell|declined|was unchanged) ([0-9.]+) percent on a seasonally adjusted basis in September",
    )
    headline_yoy = grab(
        text,
        r"Over the last 12 months, the all items index (increased|rose|decreased|fell) ([0-9.]+) percent before seasonal adjustment",
    )
    core_mom = grab(
        text,
        r"all items less food and energy (rose|increased|fell|decreased|declined|was unchanged) ([0-9.]+) percent in September",
    )
    core_yoy = grab(
        text,
        r"all items less food and energy index (rose|increased|fell|decreased) ([0-9.]+) percent over the (?:last 12 months|year)",
    )
    if core_yoy is None:
        core_yoy = grab(
            text,
            r"index for all items less food and energy (rose|increased|fell|decreased) ([0-9.]+) percent over the year",
        )
    energy_mom = grab(
        text,
        r"The energy index (increased|rose|decreased|fell|declined|was unchanged) ([0-9.]+) percent (?:over the month|in September)",
    )
    shelter_mom = grab(
        text,
        r"The shelter index (increased|rose|decreased|fell|declined|was unchanged) ([0-9.]+) percent in September",
    )
    published = bool(september and headline_mom is not None and not embargo)
    return {
        "published": published,
        "september": september,
        "embargo": embargo,
        "headline_mom": headline_mom,
        "headline_yoy": headline_yoy,
        "core_mom": core_mom,
        "core_yoy": core_yoy,
        "energy_mom": energy_mom,
        "shelter_mom": shelter_mom,
    }


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


def yield_pair(price, prev):
    def norm(v):
        if v is None:
            return None
        return v / 10.0 if v > 20 else v
    return norm(price), norm(prev)


def load_state():
    st = {
        "final_notified": False,
        "fail_notified": False,
        "empty_notified": False,
        "kospi_notified": False,
        "last_key": None,
        "parsed": {},
    }
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


def pct(v):
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "확인 불가"


def reaction(now_px, prev_px, kind):
    if now_px is None or prev_px is None or prev_px == 0:
        return "확인 불가"
    if kind == "bp":
        return f"{now_px:.2f}% (전일 {prev_px:.2f}, {(now_px - prev_px) * 100:+.0f}bp)"
    chg = (now_px - prev_px) / prev_px * 100
    return f"{now_px:,.2f} ({chg:+.2f}%)"


def classify(parsed, cfg):
    th = cfg["thresholds"]
    core = parsed.get("core_mom")
    core_yoy = parsed.get("core_yoy")
    head = parsed.get("headline_mom")
    hot = (core is not None and core >= th["hot_core_mom_min"]) or (
        core_yoy is not None and core_yoy >= th["hot_core_yoy_min"]
    )
    soft = core is not None and core <= th["soft_core_mom_max"] and (
        core_yoy is None or core_yoy <= cfg["prior"]["core_yoy"]
    )
    energy_led = head is not None and head >= th["energy_led_headline_mom_min"] and soft
    if energy_led:
        return "energy", "에너지 주도"
    if hot and not soft:
        return "hot", "근원 재가속"
    if soft and not hot:
        return "soft", "근원 둔화"
    if hot and soft:
        return "mixed", "헤드라인 혼조"
    return "mixed", "구성 확인 필요"


def conditions(tag):
    if tag == "soft":
        return (
            "10월 동결을 기본으로 유지. 12월 인상은 아직 살아 있음.",
            "달러지수 하락 또는 보합이고 10년물이 5bp 이상 내리면 동결 시나리오가 맞는 반응. 달러, 10년물이 같이 오르면 동결 가격은 되돌려진 것.",
            "KOSPI는 다음 날 금리 하락이 같이 나올 때만 반등을 유효로 봄. 금리 안 내리면 반도체 추격 금지.",
            "근원 한 방으로 비중 확대 금지. 12월 인상 확률을 0으로 읽지 마세요.",
        )
    if tag == "hot":
        return (
            "10월 인상 확률이 되살아나는 구간. 12월 인상은 기본 시나리오로 고정.",
            "달러지수 상승과 10년물 5bp 이상 상승이 같이 나오면 위험자산 축소 조건. 물가가 뜨거워도 10년물이 안 오르면 하루 반응으로 손절 금지.",
            "KOSPI 갭하락은 10년물 상승과 같이 볼 때만 축소. 금리 안 오르면 낙폭을 추격 매도하지 마세요.",
            "근원 재가속인데 반도체, 코스피 추격매수 금지.",
        )
    if tag == "energy":
        return (
            "헤드라인은 뜨겁고 근원은 둔화. 10월 동결은 유지, 12월은 에너지 지속 여부로 미룸.",
            "달러, 10년물 상승이 에너지 때문이면 근원 재가속으로 읽지 마세요. 10년물 5bp 상승은 에너지 반영인지 근원 반영인지 구분.",
            "KOSPI는 헤드라인만으로 축소 금지. 다음 날 원/달러가 급등하고 10년물이 같이 오를 때만 비중 점검.",
            "휘발유 한 달로 12월 인상 확정 처리 금지. 10월 15일 PPI를 보기 전 확대 금지.",
        )
    return (
        "구성이 엇갈림. 10월 동결, 12월 인상 둘 다 확정하지 않음.",
        "달러와 10년물이 같은 방향일 때만 시나리오를 고르세요. 엇갈리면 관망.",
        "KOSPI는 10년물 방향이 나오기 전 손대지 마세요.",
        "빠진 항목을 서프라이즈로 채우지 마세요.",
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
    if st.get("final_notified") and st.get("kospi_notified") and mode != "force":
        print("already sent final and kospi; skip")
        save_state(st, now)
        return 0
    parsed, err = {}, ""
    try:
        parsed = parse_bls(textify(fetch(cfg["sources"]["bls"])))
    except Exception as exc:
        err = str(exc)
        print("bls fail", exc)
    dxy_now, dxy_prev = yahoo(cfg["tickers"]["dxy"])
    tnx_now, tnx_prev = yield_pair(*yahoo(cfg["tickers"]["tnx"]))
    kospi_now, kospi_prev = yahoo(cfg["tickers"]["kospi"])
    krw_now, krw_prev = yahoo(cfg["tickers"]["usdkrw"])
    st["parsed"] = parsed
    st["quotes"] = {
        "dxy": [dxy_now, dxy_prev],
        "tnx": [tnx_now, tnx_prev],
        "kospi": [kospi_now, kospi_prev],
        "krw": [krw_now, krw_prev],
    }
    if mode != "dry" and not parsed:
        if not st.get("fail_notified"):
            emit(cfg, "[CPI] 조회 실패 | 물가 서프라이즈로 읽지 마", [
                now.strftime("%Y-%m-%d %H:%M KST") + " | CPI | 9월 소비자물가",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + (err or "출처 없음") + ". 서프라이즈로 처리하지 않음.",
                "지금 할 일: BLS 원문을 직접 확인. 이 실패로 매매하지 마세요.",
                "나중에: 원문이 열리면 창 안에서 한 번 더 보냅니다. 만료 2026-10-15.",
                "익절/줄일 때: 실패 알림으로 비중 조정 금지.",
                "하지 말 것: 조회 실패를 물가 둔화나 재가속으로 읽기.",
            ], 4)
            st["fail_notified"] = True
        save_state(st, now)
        return 0
    published = bool(parsed.get("published"))
    follow = st.get("final_notified") and now >= datetime.fromisoformat("2026-10-15T09:00:00").replace(tzinfo=KST)
    if follow and mode != "force":
        if st.get("kospi_notified"):
            print("kospi follow already sent")
            save_state(st, now)
            return 0
        tag = st.get("tag") or "mixed"
        call, dxy_rule, kospi_rule, dont = conditions(tag)
        lines = [
            now.strftime("%Y-%m-%d %H:%M KST") + " | CPI | 9월 소비자물가 다음날 반응",
            "이게 뭐 알림: 전날 CPI 분해 이후 KOSPI, 원/달러 반응이 조건에 맞는지 보는 후속입니다.",
            "지금 상태: 전날 판정 " + tag
            + ". 달러지수 " + reaction(dxy_now, dxy_prev, "pct")
            + " / 10년물 " + reaction(tnx_now, tnx_prev, "bp")
            + " / KOSPI " + reaction(kospi_now, kospi_prev, "pct")
            + " / 원달러 " + reaction(krw_now, krw_prev, "pct") + ".",
            "지금 할 일: " + kospi_rule,
            "나중에: 이 후속은 한 번만. 10월 15일 PPI는 별도 신호가 아님.",
            "익절/줄일 때: " + dxy_rule,
            "하지 말 것: " + dont + " 신용, 미수, 추격매수.",
        ]
        title = "[CPI] 다음날 반응 | KOSPI는 금리와 같이 볼 것"
        if mode == "dry":
            print(title)
            print("\n".join(lines))
            save_state(st, now)
            return 0
        emit(cfg, title, lines, 3)
        st["kospi_notified"] = True
        save_state(st, now)
        return 0
    if not published:
        if mode in {"force", "dry"} or now >= datetime.fromisoformat("2026-10-14T22:45:00").replace(tzinfo=KST):
            if st.get("empty_notified") and mode != "force":
                print("empty already sent")
                save_state(st, now)
                return 0
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | CPI | 9월 소비자물가",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 9월 발표문이 아직 아님. 8월 수치를 9월로 처리하지 않음.",
                "지금 할 일: 발표 확인 전 포지션 유지. 이 대기를 매수, 매도 신호로 읽지 마세요.",
                "나중에: 발표문이 열리면 창 안에서 한 번 보냅니다. 만료 2026-10-15.",
                "익절/줄일 때: 미발표 상태로 비중 조정 금지.",
                "하지 말 것: 하우스 예비 전망을 컨센서스 잠금으로 쓰기. TD 0.54, Continuum 0.6은 미잠금.",
            ]
            if mode == "dry":
                print("[CPI] 아직 9월 발표문 없음 | 대기")
                print("\n".join(lines))
                save_state(st, now)
                return 0
            emit(cfg, "[CPI] 아직 9월 발표문 없음 | 대기", lines, 3)
            st["empty_notified"] = True
            save_state(st, now)
            return 0
        print("not published yet; silent")
        save_state(st, now)
        return 0
    code, tag = classify(parsed, cfg)
    call, dxy_rule, kospi_rule, dont = conditions(code)
    prior = cfg["prior"]
    status = (
        "헤드라인 전월비 " + pct(parsed.get("headline_mom")) + f" (8월 {prior['headline_mom']})"
        + " / 전년비 " + pct(parsed.get("headline_yoy")) + f" (8월 {prior['headline_yoy']})"
        + " / 근원 전월비 " + pct(parsed.get("core_mom")) + f" (8월 {prior['core_mom']})"
        + " / 근원 전년비 " + pct(parsed.get("core_yoy")) + f" (8월 {prior['core_yoy']})"
        + " / 에너지 전월비 " + pct(parsed.get("energy_mom")) + f" (8월 {prior['energy_mom']})"
        + " / 주거 전월비 " + pct(parsed.get("shelter_mom")) + f" (8월 {prior['shelter_mom']})"
        + ". 단일 컨센서스는 미잠금."
        + " 달러지수 " + reaction(dxy_now, dxy_prev, "pct")
        + " / 10년물 " + reaction(tnx_now, tnx_prev, "bp")
        + " / KOSPI " + reaction(kospi_now, kospi_prev, "pct") + " (한국장 마감 후라 당일 반응 아님)"
        + " / 원달러 " + reaction(krw_now, krw_prev, "pct") + "."
    )
    title = f"[CPI] {tag} | {call.split('.')[0]}"
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | CPI | 9월 소비자물가",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + call + " " + dxy_rule,
        "나중에: KOSPI 조건은 10월 15일 09:10 후속에서 한 번 더 봄. PPI는 10월 15일 21:30 KST. 만료 2026-10-15.",
        "익절/줄일 때: " + kospi_rule,
        "하지 말 것: " + dont + " 신용, 미수, 추격매수. 하우스 예비치를 공식 컨센서스로 쓰기.",
        "출처: " + cfg["sources"]["bls"],
    ]
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        save_state(st, now)
        return 0
    emit(cfg, title, lines, 4)
    st["final_notified"] = True
    st["tag"] = code
    st["last_key"] = json.dumps(parsed, sort_keys=True)
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
