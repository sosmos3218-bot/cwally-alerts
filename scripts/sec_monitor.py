#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "sec.json"
STATE_PATH = ROOT / "state" / "sec.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

ZONE_KO = {
    "REDUCE": "20만미만",
    "BUY1": "20~24만",
    "BUY2": "24~28만",
    "WATCH": "28~29만/32~33만/38~41만/45~46만",
    "HOLD": "29~32만",
    "SELL1": "33~38만",
    "HOLD38": "38~41만",
    "NONEW": "41~45만",
    "SELL2": "46~50만",
    "EXIT": "50만위",
    "DATA_FAIL": "시세 조회 실패",
}
ACTION = {
    "REDUCE": "감액",
    "BUY1": "진입",
    "BUY2": "진입",
    "WATCH": "관찰",
    "HOLD": "보유",
    "SELL1": "익절",
    "HOLD38": "관찰",
    "NONEW": "관찰",
    "SELL2": "익절",
    "EXIT": "익절",
    "DATA_FAIL": "관찰",
}
DO_NOW = {
    "REDUCE": "신규 금지. 비중을 줄이세요.",
    "BUY1": "1차 30~40%만. LTA파기나 가이던스 하향이면 쉬세요.",
    "BUY2": "2차 20~30%만. 올인 금지.",
    "WATCH": "추격 금지. 구간 선만 보세요.",
    "HOLD": "보유. 29만 위 비중 증가 금지.",
    "SELL1": "1차 20~30% 익절.",
    "HOLD38": "38만 재이탈하면 감액. 신규 금지.",
    "NONEW": "신규 없음. 38만 재이탈시 감액.",
    "SELL2": "2차 익절.",
    "EXIT": "50만 위 정리.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
WATCH_LINE = {
    "REDUCE": "20만",
    "BUY1": "20만/24만",
    "BUY2": "24만/28만",
    "WATCH": "29만",
    "HOLD": "29만/32만",
    "SELL1": "33만/38만",
    "HOLD38": "38만",
    "NONEW": "38만/41만",
    "SELL2": "46만",
    "EXIT": "50만",
    "DATA_FAIL": "시세",
}

EARN_SOURCES = [
    "https://stock.naver.com/api/domestic/detail/notice?itemCode=005930&page=1&pageSize=20",
    "https://m.stock.naver.com/api/news/stock/005930?pageSize=20&page=1",
    "https://www.samsung.com/sec/ir/financial-information/earnings-release/",
    "https://news.samsung.com/kr/?s=%EC%98%81%EC%97%85%EC%9D%B4%EC%9D%B5",
]


def yahoo_close(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=10d"
    )
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        res = payload["chart"]["result"][0]
        meta = res.get("meta", {})
        quote = (res.get("indicators") or {}).get("quote") or [{}]
        closes = quote[0].get("close") or []
        last = None
        for val in reversed(closes):
            if val is not None:
                last = float(val)
                break
        price = last or meta.get("previousClose") or meta.get("regularMarketPrice")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed {ticker}: {exc}", file=sys.stderr)
        return None


def zone(price):
    if price is None:
        return "DATA_FAIL"
    if price < 200000:
        return "REDUCE"
    if price < 240000:
        return "BUY1"
    if price < 280000:
        return "BUY2"
    if price < 290000:
        return "WATCH"
    if price < 320000:
        return "HOLD"
    if price < 330000:
        return "WATCH"
    if price < 380000:
        return "SELL1"
    if price < 410000:
        return "HOLD38"
    if price < 450000:
        return "NONEW"
    if price < 460000:
        return "WATCH"
    if price < 500000:
        return "SELL2"
    return "EXIT"


def zone_label(z, price):
    if price is None:
        return ZONE_KO[z]
    if z == "REDUCE":
        return "20만미만"
    if z == "BUY1":
        return "20~24만"
    if z == "BUY2":
        return "24~28만"
    if z == "HOLD":
        return "29~32만"
    if z == "SELL1":
        return "33~38만"
    if z == "HOLD38":
        return "38~41만"
    if z == "NONEW":
        return "41~45만"
    if z == "SELL2":
        return "46~50만"
    if z == "EXIT":
        return "50만위"
    if 280000 <= price < 290000:
        return "28~29만"
    if 320000 <= price < 330000:
        return "32~33만"
    if 450000 <= price < 460000:
        return "45~46만"
    return ZONE_KO[z]


def warnings(price, prev_price):
    out = []
    if price is None:
        return out
    if price < 200000:
        out.append("20만하회")
    if price >= 290000:
        out.append("29만돌파(종가)")
    if price >= 330000:
        out.append("33만감액")
    if prev_price is not None and prev_price >= 380000 and price < 380000:
        out.append("38만재이탈")
    if price >= 410000:
        out.append("41만재평가")
    if price >= 460000:
        out.append("46만익절")
    if price >= 500000:
        out.append("50만과열")
    return out


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()
    text = raw.decode("utf-8", errors="ignore")
    if text.lstrip()[:1] in "[{":
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import naver_api

            return naver_api.flatten_text(json.loads(text))
        except (ValueError, ImportError):
            pass
    return text


def to_jo(num: float, unit: str) -> float:
    unit = unit.replace(" ", "")
    if "조" in unit:
        return num
    if "억" in unit:
        return num / 10000.0
    if "만억" in unit:
        return num / 10000.0
    return num


def parse_op_eps(text: str) -> dict:
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&nbsp;|&#\d+;", " ", clean)
    clean = re.sub(r"\s+", " ", clean)
    out = {"op": None, "eps": None, "q4_op": None, "q4_note": "", "source_hit": "", "published": False}
    if not re.search(r"삼성전자|005930", clean):
        pass
    earn_hit = re.search(
        r"(3분기|3Q|잠정실적|영업이익).{0,80}(조\s*원|억원)",
        clean,
    )
    if earn_hit:
        out["published"] = True
        out["source_hit"] = earn_hit.group(0)[:180]
    op_pats = [
        r"(?:3분기|3Q|잠정).{0,40}영업이익[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(조\s*원?|억\s*원?)",
        r"영업이익[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(조\s*원?|억\s*원?).{0,20}(?:3분기|3Q)",
        r"연결기준\s*영업이익[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(조\s*원?|억\s*원?)",
    ]
    for pat in op_pats:
        m = re.search(pat, clean, re.I)
        if m:
            val = to_jo(float(m.group(1)), m.group(2))
            if 20 <= val <= 250:
                out["op"] = val
                out["published"] = True
                out["source_hit"] = out["source_hit"] or m.group(0)[:180]
                break
    eps_pats = [
        r"(?:3분기|3Q).{0,40}(?:주당순이익|EPS)[^0-9]{0,12}(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원",
        r"(?:주당순이익|EPS)[^0-9]{0,12}(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원.{0,20}(?:3분기|3Q)",
    ]
    for pat in eps_pats:
        m = re.search(pat, clean, re.I)
        if m:
            raw = m.group(1).replace(",", "")
            val = float(raw)
            if 3000 <= val <= 40000:
                out["eps"] = val
                out["published"] = True
                break
    q4 = re.search(
        r"(?:4분기|4Q|사분기).{0,40}영업이익[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(조\s*원?|억\s*원?)",
        clean,
        re.I,
    )
    if q4:
        val = to_jo(float(q4.group(1)), q4.group(2))
        if 20 <= val <= 300:
            out["q4_op"] = val
            out["q4_note"] = q4.group(0)[:120]
    weak = re.search(r"(가이던스\s*하향|수요\s*둔화|실적\s*피크|전망\s*하향|4분기.{0,20}감소)", clean)
    strong = re.search(r"(공급\s*부족\s*지속|수급\s*타이트|가격\s*상승\s*지속|4분기.{0,20}개선)", clean)
    if weak and not out["q4_note"]:
        out["q4_note"] = "약세 코멘트"
    elif strong and not out["q4_note"]:
        out["q4_note"] = "강세 코멘트"
    return out


def collect_earnings() -> dict:
    merged = {"op": None, "eps": None, "q4_op": None, "q4_note": "", "source": "", "published": False, "fetch_ok": False}
    for url in EARN_SOURCES:
        try:
            text = fetch_text(url)
            merged["fetch_ok"] = True
        except Exception as exc:
            print(f"earn fetch fail {url}: {exc}", file=sys.stderr)
            continue
        parsed = parse_op_eps(text)
        print(f"earn parse {url} op={parsed['op']} eps={parsed['eps']} pub={parsed['published']}")
        if parsed["op"] and merged["op"] is None:
            merged["op"] = parsed["op"]
            merged["source"] = url
        if parsed["eps"] and merged["eps"] is None:
            merged["eps"] = parsed["eps"]
            merged["source"] = merged["source"] or url
        if parsed["q4_op"] and merged["q4_op"] is None:
            merged["q4_op"] = parsed["q4_op"]
        if parsed["q4_note"] and not merged["q4_note"]:
            merged["q4_note"] = parsed["q4_note"]
        if parsed["published"]:
            merged["published"] = True
            merged["source"] = merged["source"] or url
        if merged["op"] is not None:
            break
    return merged


def verdict(cfg_e: dict, got: dict) -> str:
    op, eps, q4 = got.get("op"), got.get("eps"), got.get("q4_op")
    note = got.get("q4_note") or ""
    if op is None and eps is None:
        return "PENDING"
    break_op = op is not None and op <= cfg_e["op_break"]
    break_eps = eps is not None and eps <= cfg_e["eps_break"]
    cut_op = op is not None and op < cfg_e["op_cons"]
    cut_eps = eps is not None and eps < cfg_e["eps_cons"]
    q4_weak = (q4 is not None and q4 < cfg_e["q4_op_floor"]) or ("약세" in note) or ("하향" in note)
    up_op = op is not None and op >= cfg_e["op_aug"]
    up_eps = eps is None or eps >= cfg_e["eps_aug"]
    if break_op or break_eps or (q4 is not None and q4 < cfg_e["q4_op_floor"]):
        return "CUT"
    if cut_op or cut_eps or q4_weak:
        return "CUT"
    if up_op and up_eps and not q4_weak:
        return "UP"
    return "HOLD"


def earn_copy(ver: str, got: dict, cfg_e: dict) -> tuple[str, list[str], int]:
    op = got.get("op")
    eps = got.get("eps")
    q4 = got.get("q4_op")
    op_txt = f"{op:.1f}조" if op is not None else "없음"
    eps_txt = f"{eps:,.0f}원" if eps is not None else "없음"
    q4_txt = f"{q4:.1f}조" if q4 is not None else (got.get("q4_note") or "숫자 없음")
    if ver == "CUT":
        title = f"[SEC] 연간치 하향 | 3Q {op_txt}"
        now_do = "연간 추정치가 내려갈 구간입니다. 29만 위 비중 증가 금지."
        later = "4분기 코멘트와 메모리 가격을 보고 다음 구간을 정하세요."
        pri = 5
    elif ver == "UP":
        title = f"[SEC] 연간치 상향 | 3Q {op_txt}"
        now_do = "연간치는 올라갈 수 있습니다. 그래도 29만 위 추격은 금지."
        later = "4분기가 120조를 지키는지 한 번 더 보세요."
        pri = 4
    else:
        title = f"[SEC] 연간치 유지 | 3Q {op_txt}"
        now_do = "연간치는 유지로 봅니다. 구간 규칙은 그대로입니다."
        later = "4분기 가이던스가 120조 아래로 내려오면 하향으로 바꿉니다."
        pri = 4
    lines = [
        f"이게 뭐 알림: 삼성전자 3분기 실적을 9월 21일 임계값과 대조한 연간치 판정입니다. 투자 권유 아닙니다.",
        f"지금 결과: {ver} | 3Q 영업이익 {op_txt} / EPS {eps_txt} | 4Q {q4_txt}",
        f"임계값: 유지 111조·14,000원, 하향시작 그 미만, 확산 105조·13,300원, 붕괴 100조·13,000원 또는 4Q 120조 아래.",
        f"지금 할 일: {now_do}",
        f"나중에: {later}",
        "익절/줄일 때: 33~38만 1차 20~30%, 46~50만 2차, 50만 위 정리.",
        "하지 말 것: 실적 다음날 추격, 신용/미수, 숫자 없이 연간치 상향으로 단정.",
    ]
    if got.get("source"):
        lines.append(f"출처: {got['source']}")
    return title, lines, pri


def run_earnings(cfg: dict, now: datetime, state: dict) -> dict:
    earn = cfg.get("earnings") or {}
    today = now.strftime("%Y-%m-%d")
    year = now.year
    if year != int(earn.get("year_guard", 2026)):
        print("sec earn year-guard skip")
        return state
    if today < earn.get("window_start", "2099-01-01") or today > earn.get("window_end", "2000-01-01"):
        print("sec earn outside window; silent")
        return state
    if state.get("earn_final_notified"):
        print("sec earn already final; skip")
        return state

    got = collect_earnings()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if not got.get("fetch_ok"):
        if not state.get("earn_fail_notified"):
            title = "[SEC] 실적 조회 실패 | 판정 보류"
            msg = "\n".join(
                [
                    f"{stamp} | SEC | 삼성전자 005930",
                    "이게 뭐 알림: 3분기 실적 조회가 실패했습니다. 실패를 정상으로 처리하지 않습니다.",
                    "지금 결과: 조회 실패. 연간치 판정 없음.",
                    "지금 할 일: 공시와 IR을 직접 확인하세요.",
                    "나중에: 다음 장전 슬롯에서 다시 봅니다.",
                    "익절/줄일 때: 기존 구간 규칙 유지.",
                    "하지 말 것: 조회 실패를 호재/악재로 해석.",
                ]
            )
            ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, 4, "warning")
            state["earn_fail_notified"] = True
        state["earn_status"] = "FETCH_FAIL"
        return state

    ver = verdict(earn, got)
    state["earn_op"] = got.get("op")
    state["earn_eps"] = got.get("eps")
    state["earn_q4_op"] = got.get("q4_op")
    state["earn_source"] = got.get("source")
    state["earn_status"] = ver
    state["earn_updated_at"] = stamp

    if ver == "PENDING":
        if today >= earn.get("expected_date", today) and not state.get("earn_pending_notified"):
            title = "[SEC] 실적 아직 없음 | 판정 보류"
            msg = "\n".join(
                [
                    f"{stamp} | SEC | 삼성전자 005930",
                    "이게 뭐 알림: 3분기 영업이익/EPS 숫자가 아직 안 잡혔습니다.",
                    "지금 결과: 미발표 또는 파싱 실패. 연간치 판정 없음.",
                    "지금 할 일: 공시가 나온 뒤에만 보세요. 추격 금지.",
                    "나중에: 창 안(11/3까지) 장전 슬롯에서 다시 봅니다.",
                    "익절/줄일 때: 기존 구간 규칙 유지.",
                    "하지 말 것: 없는 숫자를 맞춰 넣기.",
                ]
            )
            ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, 3, "hourglass")
            state["earn_pending_notified"] = True
        else:
            print("sec earn pending; silent")
        return state

    title, body, pri = earn_copy(ver, got, earn)
    lines = [f"{stamp} | SEC | 삼성전자 005930", *body]
    ntfy_publish("https://ntfy.sh", "cwally-port", title, "\n".join(lines), pri, "chart_with_upwards_trend")
    state["earn_final_notified"] = True
    state["earn_pending_notified"] = True
    return state


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if mode == "auto":
        mode = "weekly" if now.weekday() == 0 else "event"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {
        "zone": None,
        "price": None,
        "action": None,
        "last_alert_date": None,
        "warn": [],
        "earn_final_notified": False,
        "earn_pending_notified": False,
        "earn_fail_notified": False,
        "earn_status": None,
    }
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = yahoo_close(cfg["ticker"])
    z = zone(price)
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")
    act = ACTION[z]
    zname = zone_label(z, price)
    warn = warnings(price, prev.get("price"))
    warn_txt = ", ".join(warn) if warn else "경고 없음."
    man = None if price is None else round(price / 10000, 2)
    man_txt = f"{man}만" if man is not None else "시세없음"
    line1 = f"{act} - {man_txt}, {zname}."
    line2 = f"오늘 장중 {WATCH_LINE[z]} 선을 먼저 보고 {DO_NOW[z]}"

    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z
    today = now.strftime("%Y-%m-%d")
    already = prev.get("last_alert_date") == today
    send = False
    if z == "DATA_FAIL":
        send = not already
    elif mode == "weekly":
        send = not already
    elif zone_changed or (warn and warn != (prev.get("warn") or [])):
        send = not already
    else:
        print("no sec event; silent")

    display = cfg.get("display", "삼성전자")
    code = cfg.get("code", "005930")
    lines = [
        f"{stamp} | SEC | {display} {code}",
        "이게 뭐 알림: 삼성전자 주간 구간 감시입니다. 투자 권유 아닙니다.",
        f"지금 상태: {line1}",
        f"지금 할 일: {line2}",
        "나중에: 29만 위에서 비중을 늘리지 마세요. LTA, 2027 OP, 환원 숫자는 확인될 때만 하방으로 봅니다.",
        "익절/줄일 때: 33~38만 1차 20~30%, 46~50만 2차, 50만 위 정리. 38만 재이탈시 감액.",
        "하지 말 것: 20만 미만 신규, 29만 위 추격/비중증가, 올인, 신용/미수.",
        f"경고: {warn_txt}",
    ]
    msg = "\n".join(lines)
    print(msg)
    if send:
        title = f"[SEC] {act} {man_txt} {zname}"
        pri = 5 if z in {"REDUCE", "EXIT"} or "20만하회" in warn or "50만과열" in warn else (4 if warn or z in {"SELL1", "SELL2"} else 3)
        ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, pri, "chart_with_upwards_trend")

    prev = run_earnings(cfg, now, prev)
    prev.update(
        {
            "updated_at": stamp,
            "zone": z,
            "price": price,
            "action": act,
            "last_alert_date": today if send else prev.get("last_alert_date"),
            "warn": warn,
        }
    )
    STATE_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
