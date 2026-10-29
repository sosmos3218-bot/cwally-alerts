#!/usr/bin/env python3
"""Samsung Electronics Preferred 005935 discount rate monitor.

Discount = (common - pref) / common * 100, both from the KRX regular session.

Zones (configs/005935.json, revised 2026-10-28 when the discount was ~21.7%):
  EXPAND  discount >= entry_discount (26)       -> 확대 검토
  UPPER   hold_max (24) <= discount < entry      -> 관찰(상단)
  HOLD    hold_min (19) <  discount < hold_max   -> 유지
  LOWER   exit (16) <  discount <= hold_min      -> 관찰(하단)
  REDUCE  discount <= exit_discount (16)         -> 축소 검토

Price source: Daum KRX daily rows (KRX only, no NXT mix; the same source as doosan_monitor).
During the session today's row is the live KRX price, after the close it is the KRX close.
Fallback: Naver polling quote (`closePrice` is KRX during the session, may carry
after-hours prices after 15:30; labelled as such).

Modes (argv[1]):
  (none)  morning slot: latest *completed* KRX close. Sends only on a zone change or the
          first run of an ISO week.
  auto    one-shot (oneshot_pick passes "auto"): live KRX price, always sends.
  dry     print what would be sent; no publish, no state write.
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
NAVER = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
UA = "Mozilla/5.0 (Macintosh; cwally-alerts)"

ZONE_LABEL = {
    "EXPAND": "확대 검토",
    "UPPER": "관찰(상단)",
    "HOLD": "유지",
    "LOWER": "관찰(하단)",
    "REDUCE": "축소 검토",
}


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


def naver_quote(code: str) -> dict | None:
    raw = http_json(NAVER.format(code=code))
    d = (raw.get("datas") or [{}])[0]
    price = to_num(d.get("closePrice"))
    chg = to_num(d.get("compareToPreviousClosePrice"))
    if not price:
        return None
    traded = str(d.get("localTradedAt") or "")
    return {
        "day": traded[:10].replace("-", ""),
        "price": price,
        "prev": price - chg if chg is not None else None,
        "status": d.get("marketStatus"),
    }


def quote(code: str, live: bool, today: str, now: datetime) -> dict | None:
    """Return {price, prev, day, basis, source}. basis: 장중/KRX 종가/전일 KRX 종가."""
    session_open = now.hour * 60 + now.minute >= 9 * 60
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
        q = naver_quote(code)
    except Exception as exc:
        print(f"naver fail {code}: {exc}")
        q = None
    if not q:
        return None
    if q["day"] == today and session_open and not session_over:
        basis = "장중"
    elif q["day"] == today:
        basis = "종가(시간외 섞일 수 있음)"
    else:
        basis = "전일 종가"
    q.update({"basis": basis, "source": "네이버(대체)"})
    return q


def zone_of(d: float, cfg: dict) -> str:
    if d >= float(cfg["entry_discount"]):
        return "EXPAND"
    if d >= float(cfg["hold_max"]):
        return "UPPER"
    if d > float(cfg["hold_min"]):
        return "HOLD"
    if d > float(cfg["exit_discount"]):
        return "LOWER"
    return "REDUCE"


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
                    "지금 결과/상태: Daum KRX와 네이버 대체 시세가 모두 실패했거나 날짜가 서로 달라요.\n"
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
    zone = zone_of(discount, cfg)
    last_zone = state.get("last_zone")
    if last_zone is None and state.get("last_discount") is not None:
        last_zone = zone_of(float(state["last_discount"]), cfg)
    changed = last_zone is not None and zone != last_zone
    iso = now.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    weekly_due = now.weekday() == 0 and state.get("last_weekly") != week_key and not force
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
        if sent and weekly_due:
            state["last_weekly"] = week_key
        save_state(state)

    if not (force or changed or weekly_due or dry):
        remember(False)
        return 0

    e, hmax, hmin, x = (cfg["entry_discount"], cfg["hold_max"], cfg["hold_min"], cfg["exit_discount"])
    basis = qp["basis"] + (f" {now.strftime('%H:%M')}" if qp["basis"] == "장중" else f" ({qp['day'][4:6]}/{qp['day'][6:]})")
    tag = " (원샷)" if force else (" (주간 점검)" if weekly_due and not changed else "")
    title = f"{cfg['title_prefix']} 괴리율 {discount:.1f}% | {action}{tag}"
    move = ""
    if prev_disc is not None and qp["basis"] != "전일 KRX 종가":
        move = f", 전일 KRX 종가 기준 {prev_disc:.1f}%에서 {discount - prev_disc:+.1f}%p"
    lines = [
        "이게 뭐 알림: 삼성전자우 005935 괴리율 점검. 배당/장기 보유 + 상대가치 관점."
        + (" 실적 발표회 전후 원샷." if force else ""),
        f"지금 결과/상태: {basis} 우선주 {pref:,.0f}원, 보통주 {common:,.0f}원, 괴리율 {discount:.1f}%{move}. 시세 {qp['source']}.",
        f"현재 구간: 확대 {e:g}% 이상 / 관찰 {hmax:g}~{e:g}% / 유지 {hmin:g}~{hmax:g}% / 관찰 {x:g}~{hmin:g}% / 축소 {x:g}% 이하. 판단: {action}",
        "지금 할 일: " + {
            "EXPAND": "우선주 비중 확대 검토. 분할로만.",
            "UPPER": "확대 준비. 26% 넘으면 분할 확대 검토.",
            "HOLD": "기존 비중 유지. 우선주 소각 발표 여부 확인.",
            "LOWER": "신규 확대 중단. 16% 아래면 축소 검토.",
            "REDUCE": "우선주 비중 축소 또는 보통주 교체 검토.",
        }[zone],
        "나중에: 2027년 1월 이사회의 잔여 주주환원(우선주 배정 비율) 확인. 괴리율 2~3%p 변동 시 재확인.",
        f"익절/줄일 때: {x:g}% 이하 축소, 10% 이하(2015년 우선주 매입 중단선) 강한 축소.",
        "하지 말 것: 괴리율만으로 즉시 매매. 실적, 환원 발표와 함께 확인.",
    ]
    if changed:
        lines.insert(2, f"바뀐 점: 구간이 {ZONE_LABEL.get(last_zone, last_zone)}에서 {action}(으)로 바뀜")
    body = "\n".join(lines)
    if dry:
        print("DRY RUN")
        print(title)
        print(body)
        return 0
    try:
        publish("cwally-port", title, body, 3, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))
        print("sent:", title)
    except Exception as exc:
        print("publish failed", exc)
        return 1
    remember(True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else ""))
