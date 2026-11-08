#!/usr/bin/env python3
"""Samsung Electronics Preferred 005935 discount rate monitor.

Discount = (common - pref) / common * 100, both from the KRX regular session.

Zones (configs/005935.json, revised 2026-10-30, chat approval t21u; discount = 1 - pref/common, KRX close):
  WIDE    discount >= wide (25)            -> 우선주 할인 확대, 우선주 분할 매수 유리 구간 (p4)
  HOLD    narrow (20) < discount < wide    -> 유지 (p3 when entering from another zone)
  NARROW  discount <= narrow (20)          -> 우선주 우위 대부분 소진, 보통주와 다시 비교 (p4)
Hysteresis 0.5%p: leaving WIDE needs < 24.5, leaving NARROW needs > 20.5.
Old five-zone names in state (EXPAND/UPPER/LOWER/REDUCE) are migrated from last_discount without an alert.

Price source: Daum KRX daily rows (KRX only, no NXT mix; the same source as doosan_monitor).
During the session today's row is the live KRX price, after the close it is the KRX close.
Fallback: Naver 15:30 minute bar via krx_close.minute_bars (completed KRX closes only, no NXT;
2026-11-08, replaces the Naver polling quote that could carry after-hours prices).

Modes (argv[1]):
  (none)  morning slot: latest *completed* KRX close. Sends only on a zone change or the
          first run of an ISO week.
  auto    one-shot (oneshot_pick passes "auto"): live KRX price, always sends.
  dry     print what would be sent; no publish, no state write.
  selftest  synthetic zone and hysteresis cases, no network.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "005935.json"
STATE_PATH = ROOT / "state" / "005935.json"
DAUM = "https://finance.daum.net/api/quote/A{code}/days?symbolCode=A{code}&page=1&perPage=5&pagination=true"
UA = "Mozilla/5.0 (Macintosh; cwally-alerts)"

ZONE_LABEL = {
    "WIDE": "우선주 할인 확대 (25% 이상), 우선주 분할 매수 유리 구간",
    "HOLD": "유지",
    "NARROW": "우선주 우위 대부분 소진 (20% 이하), 보통주와 다시 비교",
}
ZONE_PRI = {"WIDE": 4, "HOLD": 3, "NARROW": 4}
OLD_ZONES = {"EXPAND", "UPPER", "LOWER", "REDUCE"}


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_json(url: str, referer: str | None = None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def to_num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", "").strip())
    except ValueError:
        return None


def daum_rows(code: str) -> list[dict]:
    raw = http_json(DAUM.format(code=code), referer=f"https://finance.daum.net/quotes/A{code}")
    out = []
    for r in raw.get("data") or []:
        day = str(r.get("date") or "")[:10].replace("-", "")
        price = to_num(r.get("tradePrice"))
        if day and price:
            out.append({"day": day, "price": price, "prev": to_num(r.get("prevClosingPrice"))})
    return out  # newest first


def minute_quote(code: str) -> dict | None:
    """Daum fallback: latest completed KRX close from the Naver 15:30 minute bar (krx_close)."""
    import krx_close

    bars = krx_close.minute_bars(code, 2)
    if not bars:
        return None
    last = bars[-1]
    return {
        "day": krx_close.ymd(last.day),
        "price": last.close,
        "prev": bars[-2].close if len(bars) > 1 else None,
    }


def quote(code: str, live: bool, today: str, now: datetime) -> dict | None:
    """Return {price, prev, day, basis, source}. basis: 장중/KRX 종가/전일 KRX 종가."""
    session_over = now.hour * 60 + now.minute >= 15 * 60 + 31
    try:
        rows = daum_rows(code)
    except Exception as exc:
        print(f"daum fail {code}: {exc}")
        rows = []
    if rows:
        top = rows[0]
        if top["day"] == today and not live:
            # morning slot wants the latest completed close, not the opening ticks
            prev_row = rows[1] if len(rows) > 1 else None
            if top["prev"]:
                return {"price": top["prev"], "prev": prev_row["prev"] if prev_row else None,
                        "day": prev_row["day"] if prev_row else "", "basis": "전일 KRX 종가", "source": "Daum(KRX)"}
        if top["day"] == today:
            basis = "KRX 종가" if session_over else "장중"
        else:
            basis = "전일 KRX 종가"
        return {"price": top["price"], "prev": top["prev"], "day": top["day"], "basis": basis, "source": "Daum(KRX)"}
    try:
        q = minute_quote(code)
    except Exception as exc:
        print(f"minute fallback fail {code}: {exc}")
        q = None
    if not q:
        return None
    basis = "KRX 종가" if q["day"] == today else "전일 KRX 종가"
    q.update({"basis": basis, "source": "네이버 15:30 분봉(대체)"})
    return q


def zone_of(d: float, cfg: dict, prev: str | None = None) -> str:
    wide, narrow, h = float(cfg["wide_discount"]), float(cfg["narrow_discount"]), float(cfg.get("hysteresis_pp", 0.5))
    if prev == "WIDE" and d >= wide - h:
        return "WIDE"
    if prev == "NARROW" and d <= narrow + h:
        return "NARROW"
    if d >= wide:
        return "WIDE"
    if d <= narrow:
        return "NARROW"
    return "HOLD"


def migrate_zone(state: dict, cfg: dict) -> str | None:
    """old five-zone names -> new three zones from last_discount, silently."""
    z = state.get("last_zone")
    if (z is None or z in OLD_ZONES) and state.get("last_discount") is not None:
        return zone_of(float(state["last_discount"]), cfg)
    return z


def disc(common: float | None, pref: float | None) -> float | None:
    if not common or not pref or common <= 0:
        return None
    return (common - pref) / common * 100


def run(mode: str = "") -> int:
    dry = mode == "dry"
    force = mode not in ("", "dry")
    cfg = load_json(CFG_PATH)
    state = load_json(STATE_PATH)
    now = datetime.now(KST)
    yg = cfg.get("year_guard") or 2026
    years = {int(y) for y in yg} if isinstance(yg, list) else {int(yg)}
    ends = cfg.get("ends_kst")
    if now.year not in years or (ends and now > datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST)):
        print("005935 year-guard / ends_kst silent")
        return 0
    if now.weekday() >= 5 and not force and not dry:
        return 0
    today = now.strftime("%Y%m%d")
    live = force or dry
    qp = quote(cfg["ticker"], live, today, now)
    qc = quote(cfg["ticker_common"], live, today, now)
    if not qp or not qc or qp.get("day") != qc.get("day"):
        print(f"price fetch failed or day mismatch: pref={qp} common={qc}")
        if force and not dry:
            try:
                publish(
                    "cwally-ops",
                    f"{cfg['title_prefix']} 시세 조회 실패 | 원샷 결과 없음",
                    "이게 뭐 알림: 삼성전자우 괴리율 원샷이 시세를 못 읽었어요.\n"
                    "지금 결과/상태: Daum KRX와 15:30 분봉 대체 시세가 모두 실패했거나 날짜가 서로 달라요.\n"
                    "지금 할 일: 증권 앱에서 두 종목 현재가를 직접 보세요.\n"
                    "나중에: 다음 원샷이나 평일 09:00 슬롯에서 다시 확인해요.\n"
                    "익절/줄일 때: 해당 없음.\n"
                    "하지 말 것: 이 알림을 매매 신호로 보지 마세요.",
                    3,
                    ["warning"],
                    resolve_server(cfg.get("ntfy_server")),
                )
            except Exception as exc:
                print("ops publish failed", exc)
        return 0
    pref, common = qp["price"], qc["price"]
    discount = disc(common, pref)
    prev_disc = disc(qc.get("prev"), qp.get("prev"))
    last_zone = migrate_zone(state, cfg)
    zone = zone_of(discount, cfg, last_zone)
    changed = last_zone is not None and zone != last_zone
    iso = now.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    weekly_due = state.get("last_weekly") != week_key and not force
    action = ZONE_LABEL[zone]

    def remember(sent: bool) -> None:
        if dry:
            return
        state.update({
            "last_discount": discount,
            "last_pref": pref,
            "last_common": common,
            "last_basis": qp["basis"],
            "last_source": qp["source"],
            "last_day": qp["day"],
            "updated": now.isoformat(),
        })
        if sent or last_zone is None:
            state["last_zone"] = zone
        elif state.get("last_zone") in OLD_ZONES:
            state["last_zone"] = last_zone  # migrated, no alert
        if sent and weekly_due:
            state["last_weekly"] = week_key
        save_state(state)

    if not (force or changed or weekly_due or dry):
        remember(False)
        return 0

    wide, narrow, h = float(cfg["wide_discount"]), float(cfg["narrow_discount"]), float(cfg.get("hysteresis_pp", 0.5))
    basis = qp["basis"] + (f" {now.strftime('%H:%M')}" if qp["basis"] == "장중" else f" ({qp['day'][4:6]}/{qp['day'][6:]})")
    tag = " (원샷)" if force else (" (주간 점검)" if weekly_due and not changed else "")
    title = f"{cfg['title_prefix']} 괴리율 {discount:.1f}% | {action}{tag}"
    move = ""
    if prev_disc is not None and qp["basis"] != "전일 KRX 종가":
        move = f", 전일 KRX 종가 기준 {prev_disc:.1f}%에서 {discount - prev_disc:+.1f}%p"
    lines = [
        "이게 뭐 알림: 삼성전자우 005935 할인율(1 - 우선주/보통주, KRX 종가) 점검. 우선주와 보통주 중 어느 쪽을 살지 보는 상대가치 알림."
        + (" 원샷." if force else ""),
        f"지금 결과/상태: {basis} 우선주 {pref:,.0f}원, 보통주 {common:,.0f}원, 할인율 {discount:.1f}%{move}. 시세 {qp['source']}.",
        f"현재 구간: {wide:g}% 이상 할인 확대 / {narrow:g}~{wide:g}% 유지 / {narrow:g}% 이하 우위 소진 (벗어날 때 {h:g}%p 여유: {wide - h:g}% 미만, {narrow + h:g}% 초과). 판단: {action}",
        "지금 할 일: " + {
            "WIDE": "우선주 분할 매수 유리 구간. 한 번에 사지 말고 나눠서.",
            "HOLD": "유지. 새로 사면 보통주와 우선주 중 할인율 기준으로 고르기.",
            "NARROW": "우선주 가격 이점이 거의 없음. 새로 살 때는 보통주와 다시 비교.",
        }[zone],
        "나중에: 2027년 1월 이사회의 주주환원 결정(우선주 매입 비중)은 [SEC-PREF] 알림이 따로 보냄. 2년 뒤 손익분기 할인율 약 22.7~23.4%.",
        f"익절/줄일 때: {narrow:g}% 이하면 우선주 우위가 대부분 사라짐. 보통주 전환 검토.",
        "하지 말 것: 할인율만으로 즉시 매매. 실적, 주주환원 공시와 함께 확인.",
    ]
    if changed:
        lines.insert(2, f"바뀐 점: 구간이 {ZONE_LABEL.get(last_zone, last_zone)}에서 {action}(으)로 바뀜")
    body = "\n".join(lines)
    if dry:
        print("DRY RUN (no ntfy, no state write)")
        print(f"zone={zone} last_zone={last_zone} changed={changed} weekly_due={weekly_due} priority={ZONE_PRI[zone] if changed else 3}")
        print(title)
        print(body)
        return 0
    try:
        pri = ZONE_PRI[zone] if changed else 3
        publish("cwally-port", title, body, pri, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))
        print("sent:", title)
    except Exception as exc:
        print("publish failed", exc)
        return 1
    remember(True)
    return 0


def selftest() -> int:
    cfg = load_json(CFG_PATH)
    ok, fails = 0, []

    def check(label, got, want):
        nonlocal ok
        if got == want:
            ok += 1
        else:
            fails.append(f"{label}: got {got!r} want {want!r}")

    check("HOLD 25.2 -> WIDE", zone_of(25.2, cfg, "HOLD"), "WIDE")
    check("WIDE 24.7 stays", zone_of(24.7, cfg, "WIDE"), "WIDE")
    check("WIDE 24.5 stays (edge)", zone_of(24.5, cfg, "WIDE"), "WIDE")
    check("WIDE 24.3 -> HOLD", zone_of(24.3, cfg, "WIDE"), "HOLD")
    check("HOLD 24.7 stays HOLD", zone_of(24.7, cfg, "HOLD"), "HOLD")
    check("HOLD 25.0 -> WIDE (edge)", zone_of(25.0, cfg, "HOLD"), "WIDE")
    check("HOLD 19.9 -> NARROW", zone_of(19.9, cfg, "HOLD"), "NARROW")
    check("HOLD 20.0 -> NARROW (edge)", zone_of(20.0, cfg, "HOLD"), "NARROW")
    check("NARROW 20.4 stays", zone_of(20.4, cfg, "NARROW"), "NARROW")
    check("NARROW 20.6 -> HOLD", zone_of(20.6, cfg, "NARROW"), "HOLD")
    check("HOLD 20.4 stays HOLD", zone_of(20.4, cfg, "HOLD"), "HOLD")
    check("WIDE 19.5 -> NARROW (gap)", zone_of(19.5, cfg, "WIDE"), "NARROW")
    check("priorities", (ZONE_PRI["WIDE"], ZONE_PRI["HOLD"], ZONE_PRI["NARROW"]), (4, 3, 4))
    check("migrate HOLD stays", migrate_zone({"last_zone": "HOLD", "last_discount": 21.98}, cfg), "HOLD")
    check("migrate UPPER 24.4 -> HOLD", migrate_zone({"last_zone": "UPPER", "last_discount": 24.4}, cfg), "HOLD")
    check("migrate EXPAND 26.1 -> WIDE", migrate_zone({"last_zone": "EXPAND", "last_discount": 26.1}, cfg), "WIDE")
    check("migrate LOWER 18.0 -> NARROW", migrate_zone({"last_zone": "LOWER", "last_discount": 18.0}, cfg), "NARROW")
    check("migrate none + 21.8 -> HOLD", migrate_zone({"last_discount": 21.8}, cfg), "HOLD")
    check("disc 1 - pref/common", round(disc(222500, 173600), 2), round((1 - 173600 / 222500) * 100, 2))
    texts = list(ZONE_LABEL.values()) + [json.dumps(cfg, ensure_ascii=False), Path(__file__).read_text(encoding="utf-8")]
    check("no middle dot / em-dash", [t[:30] for t in texts if "\u00b7" in t or "\u2014" in t], [])
    for f in fails:
        print("FAIL", f)
    print(f"selftest {ok}/{ok + len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    _m = sys.argv[1] if len(sys.argv) > 1 else ""
    raise SystemExit(selftest() if _m == "selftest" else run(_m))
