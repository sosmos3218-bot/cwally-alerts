#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "yen489.json"
STATE_PATH = ROOT / "state" / "yen489.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_state() -> dict:
    state = {
        "updated_at": None,
        "fx_key": None,
        "rate_key": None,
        "fail_sent_date": None,
        "last_jpy100": None,
        "last_low_52w": None,
        "last_kr_policy": None,
        "last_jp_policy": None,
        "last_bok": None,
        "last_boj": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def yahoo_closes(ticker: str) -> list[float]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker + "?interval=1d&range=1y"
    payload = json.loads(fetch(url))
    result = payload["chart"]["result"][0]
    raw = result["indicators"]["quote"][0]["close"]
    closes = [float(x) for x in raw if x is not None]
    if len(closes) < 20:
        raise RuntimeError(f"short history {ticker}")
    return closes


def jpy100_series(usdkrw: list[float], usdjpy: list[float]) -> list[float]:
    n = min(len(usdkrw), len(usdjpy))
    out: list[float] = []
    for i in range(-n, 0):
        if usdjpy[i] == 0:
            continue
        out.append(usdkrw[i] / usdjpy[i] * 100.0)
    if not out:
        raise RuntimeError("empty jpy100 series")
    return out


def bok_stance(day: datetime) -> str:
    pages = [
        "https://www.bok.or.kr/portal/bbs/B0000245/list.do?menuNo=200761",
        "https://www.bok.or.kr/portal/main/main.do",
    ]
    blob = ""
    for url in pages:
        try:
            blob += " " + fetch(url, timeout=15)
        except Exception as exc:
            print(f"bok fetch fail {url}: {exc}", file=sys.stderr)
    if not blob.strip():
        return "조회 실패"
    text = re.sub(r"<[^>]+>", " ", blob)
    text = re.sub(r"\s+", " ", text)
    marks = (day.strftime("%Y.%m.%d"), f"{day.month}월 {day.day}일", day.strftime("%Y-%m-%d"))
    if not any(m in text for m in marks):
        return "공표문 아직 없음"
    if re.search(r"인하|lower(ed)? the base rate|cut (the )?rate", text, flags=re.I):
        return "금리 인하"
    if re.search(r"인상|raise(d)? the base rate|hike", text, flags=re.I):
        return "금리 인상"
    if re.search(r"동결|maintain|unchanged|keep(ing)? the base rate", text, flags=re.I):
        return "금리 동결"
    return "문장 확인 실패"


def boj_stance(day: datetime) -> str:
    pages = [
        "https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/index.htm",
        "https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/index.htm",
    ]
    blob = ""
    for url in pages:
        try:
            blob += " " + fetch(url, timeout=15)
        except Exception as exc:
            print(f"boj fetch fail {url}: {exc}", file=sys.stderr)
    if not blob.strip():
        return "조회 실패"
    text = re.sub(r"<[^>]+>", " ", blob)
    text = re.sub(r"\s+", " ", text)
    ymd = day.strftime("%y%m%d")
    day_ko = f"{day.month}月{day.day}日"
    if day_ko not in text and ymd not in text:
        return "공표문 아직 없음"
    if "金融市場調節方針の変更" in text or re.search(r"raise|hike|利上げ", text, flags=re.I):
        return "금리 인상"
    if re.search(r"lower|cut|利下げ", text, flags=re.I):
        return "금리 인하"
    if "当面の金融政策運営" in text or re.search(r"maintain|unchanged|現状維持", text, flags=re.I):
        return "금리 동결"
    return "문장 확인 실패"


def send(cfg: dict, title: str, lines: list[str], priority: int, dry: bool) -> None:
    print("WOULD_SEND" if dry else "SEND", title)
    print("\n".join(lines))
    if dry:
        return
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["jp", "chart_with_upwards_trend"], server)


def fail_body(stamp: str, why: str) -> list[str]:
    return [
        f"{stamp} | 489000 | 점검 실패",
        "이게 무 알림: PLUS 일본엔화초단기국채(489000)의 엔강·한일 금리차 조건 점검이다.",
        f"지금 결과/상태: 조회 실패. 조건 미충족이 아니다. 사유: {why}",
        "지금 할 일: 원/엔과 한은·일한 정책금리만 수동으로 확인한다.",
        "나중에: 데이터가 나오면 다음날 09:00에 다시 판정한다.",
        "익절/줄일 때: 실패만으로 489000을 사거나 팔지 않는다.",
        "하지 말 것: 재시도 알림을 연속으로 보내지 않는다. 장중 추정으로 채우지 않는다.",
    ]


def fx_body(stamp: str, status: str, do_now: str) -> list[str]:
    return [
        f"{stamp} | 489000 | 엔화",
        "이게 무 알림: 489000 재검토 조건 1. 원/엔이 저점권을 벗어나거나 의미 있게 엔강한지 본다.",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {do_now}",
        "나중에: 같은 단계는 다시 안 보낸다. 더 센 단계(880원 이탈 → 900원 엔강)만 추가 발송.",
        "익절/줄일 때: 100엔이 다시 880원 아래로 내려가면 신규 매수를 먼춠다.",
        "하지 말 것: 알림만 보고 추격 매수. 엔 강세=원 강세로 같이 베팅. 신용·미수.",
    ]


def rate_body(stamp: str, status: str, do_now: str) -> list[str]:
    return [
        f"{stamp} | 489000 | 금리차",
        "이게 무 알림: 489000 재검토 조건 2. 한일 단기 정책금리 차가 줄었는지 본다.",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {do_now}",
        "나중에: 다음 한은 10/22·11/26, 일한 10/30·12/18. 같은 단계는 다시 안 보낸다.",
        "익절/줄일 때: 금리차가 다시 벌어지면 캐리 열위가 복원된 것이다. 환차익만 남는다.",
        "하지 말 것: 문장 확인 실패를 인하·인상으로 단정. 시장금리만 보고 정책 변경으로 취급.",
    ]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"dry", "check", "nosend"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    state = load_state()

    if now.year != int(cfg.get("oneshot_year") or 2026):
        print("yen489 year-guard silent")
        return 0
    expire = datetime.strptime(cfg.get("expire_date") or "2026-12-31", "%Y-%m-%d").date()
    if now.date() > expire:
        print("yen489 expired silent")
        return 0
    if now.weekday() >= 5 and mode not in {"force", "dry", "check", "nosend"}:
        print("weekend silent")
        return 0

    fx_spec = cfg["fx"]
    rate_spec = cfg["rates"]
    as_of = float(cfg["as_of_jpy100"])
    leave_krw = float(fx_spec.get("leave_krw") or 880)
    strength_krw = float(fx_spec.get("strength_krw") or 900)
    kr0 = float(rate_spec["baseline_kr_policy"])
    jp0 = float(rate_spec["baseline_jp_policy"])
    shrink_bp = float(rate_spec["shrink_bp"])
    gap0 = kr0 - jp0

    jpy100 = low52 = None
    fx_fail = False
    try:
        usdkrw = yahoo_closes(cfg["tickers"]["usdkrw"])
        usdjpy = yahoo_closes(cfg["tickers"]["usdjpy"])
        series = jpy100_series(usdkrw, usdjpy)
        jpy100 = series[-1]
        low52 = min(series)
    except Exception as exc:
        print(f"fx fetch fail: {exc}", file=sys.stderr)
        fx_fail = True

    bok_dates = set(cfg.get("bok_dates") or [])
    boj_dates = set(cfg.get("boj_dates") or [])
    kr_pol = float(state.get("last_kr_policy") or kr0)
    jp_pol = float(state.get("last_jp_policy") or jp0)
    bok = state.get("last_bok")
    boj = state.get("last_boj")
    rate_fail = False

    if today in bok_dates:
        bok = bok_stance(now)
        if bok == "조회 실패":
            rate_fail = True
        elif bok == "금리 인하":
            kr_pol = kr0 - 0.25
        elif bok == "금리 인상":
            kr_pol = kr0 + 0.25
        elif bok == "금리 동결":
            kr_pol = kr0
    if today in boj_dates:
        boj = boj_stance(now)
        if boj == "조회 실패":
            rate_fail = True
        elif boj == "금리 인상":
            jp_pol = jp0 + 0.25
        elif boj == "금리 인하":
            jp_pol = jp0 - 0.25
        elif boj == "금리 동결":
            jp_pol = jp0

    gap = kr_pol - jp_pol
    kr_cut = kr_pol < kr0 - 1e-9
    jp_hike = jp_pol > jp0 + 1e-9
    gap_shrink = gap <= gap0 - shrink_bp / 100.0 + 1e-9
    rate_hit = kr_cut or jp_hike or gap_shrink

    leave = False
    strong = False
    if jpy100 is not None:
        leave = jpy100 >= leave_krw
        strong = jpy100 >= strength_krw

    print(stamp, {"jpy100": jpy100, "low52": low52, "leave_krw": leave_krw, "strength_krw": strength_krw, "leave": leave, "strong": strong}, {"kr": kr_pol, "jp": jp_pol, "gap": gap, "gap0": gap0, "shrink_bp": shrink_bp, "bok": bok, "boj": boj, "rate_hit": rate_hit}, mode)

    would = []

    if fx_fail:
        if state.get("fail_sent_date") != today:
            send(cfg, "[489000] 조회실패 | 원엔 미확정", fail_body(stamp, "원/엔 시계열"), 4, dry)
            would.append("fx_fail")
            if not dry:
                state["fail_sent_date"] = today
        else:
            print("duplicate fx fail; silent")
        if not dry:
            state["updated_at"] = stamp
            save_state(state)
        print("DRY_RESULT", {"would_send": would, "fx": "fail", "rate": "skipped"} if dry else "saved")
        return 0

    fx_key = None
    if strong:
        fx_key = "strong"
    elif leave:
        fx_key = "leave"
    if fx_key and state.get("fx_key") != fx_key:
        if fx_key == "strong":
            status = f"100엔 {jpy100:.2f}원 | 선 900원 | 52주 저점 {low52:.2f}원 | 기준일 {as_of:.2f}원 대비 {(jpy100 / as_of - 1) * 100:+.2f}%"
            do_now = "의미 있는 엔강(900원). 489000을 환차익 도구로 재검토한다. 한 번에 전량 넣지 말고 분할."
            title = "[489000] 엔강 확인 | 분할 재검토"
        else:
            status = f"100엔 {jpy100:.2f}원 | 선 880원 | 52주 저점 {low52:.2f}원 | 기준일 {as_of:.2f}원 대비 {(jpy100 / as_of - 1) * 100:+.2f}%"
            do_now = "저점권만 벗어났다(880원). 추격 매수 금지. 의미 있는 엔강(900원)까지 기다린다."
            title = "[489000] 저점권 이탈 | 추격 금지"
        send(cfg, title, fx_body(stamp, status, do_now), 4 if fx_key == "strong" else 3, dry)
        would.append(fx_key)
        if not dry:
            state["fx_key"] = fx_key
    else:
        print("fx silent", fx_key, state.get("fx_key"))

    if rate_fail and today in (bok_dates | boj_dates):
        if state.get("fail_sent_date") != today:
            send(cfg, "[489000] 조회실패 | 금리 미확정", fail_body(stamp, "한은 또는 일한 공표문"), 4, dry)
            would.append("rate_fail")
            if not dry:
                state["fail_sent_date"] = today
        else:
            print("duplicate rate fail; silent")
    elif rate_hit:
        why = []
        if kr_cut:
            why.append(f"한은 인하({bok or '반영'})")
        if jp_hike:
            why.append(f"일한 인상({boj or '반영'})")
        if gap_shrink:
            why.append(f"금리차 {gap0:.2f}%p → {gap:.2f}%p")
        rate_key = "|".join(why) or f"gap-{gap:.2f}"
        if state.get("rate_key") != rate_key:
            status = (
                f"한은 {kr_pol:.2f}% / 일한 {jp_pol:.2f}% / 차 {gap:.2f}%p "
                f"(기준 {gap0:.2f}%p, 축소선 {shrink_bp:.0f}bp). "
                + (", ".join(why) if why else "조건 충족")
            )
            do_now = "캐리 열위가 줄었다. 489000은 여전히 환노출이다. 환율 조건과 같이 보고 분할만."
            send(cfg, "[489000] 금리차 축소 | 분할 재검토", rate_body(stamp, status, do_now), 4, dry)
            would.append("rate")
            if not dry:
                state["rate_key"] = rate_key
        else:
            print("rate same key; silent")
    else:
        print("rate silent; condition not met")

    if dry:
        print("DRY_RESULT", {"would_send": would or ["none"], "persist": False})
        return 0

    state["updated_at"] = stamp
    state["last_jpy100"] = jpy100
    state["last_low_52w"] = low52
    state["last_kr_policy"] = kr_pol
    state["last_jp_policy"] = jp_pol
    state["last_bok"] = bok
    state["last_boj"] = boj
    if not fx_fail:
        state["fail_sent_date"] = None
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
