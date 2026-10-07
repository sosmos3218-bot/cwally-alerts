#!/usr/bin/env python3
"""CEG change-condition watch. Silent unless price zone or earnings window hits."""
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
CFG_PATH = ROOT / "configs" / "ceg.json"
STATE_PATH = ROOT / "state" / "ceg.json"
UA = {"User-Agent": "cwally-alerts ceg monitor research@example.com"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def quote(url: str):
    try:
        data = json.loads(fetch(url))
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None, "quote empty"
        price = (result[0].get("meta") or {}).get("regularMarketPrice")
        return (float(price) if price is not None else None), None
    except Exception as exc:
        return None, str(exc)


def visible(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def latest_8k(url: str):
    data = json.loads(fetch(url))
    recent = ((data.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    docs = recent.get("primaryDocument") or []
    acc = recent.get("accessionNumber") or []
    for form, day, doc, accession in zip(forms, dates, docs, acc):
        if form == "8-K":
            acc_nodash = accession.replace("-", "")
            link = f"https://www.sec.gov/Archives/edgar/data/1868275/{acc_nodash}/{doc}"
            return day, link
    return None, None


def parse_guide(blob: str) -> dict:
    lo = hi = None
    m = re.search(
        r"adjusted \(non-gaap\) operating earnings[^.]{0,120}?\$(\d+\.\d+)\s*(?:to|–|-)\s*\$(\d+\.\d+)",
        blob,
        re.I,
    )
    if not m:
        m = re.search(
            r"adjusted operating earnings guidance[^.]{0,80}?\$(\d+\.\d+)\s*(?:to|–|-)\s*\$(\d+\.\d+)",
            blob,
            re.I,
        )
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
    cut = bool(re.search(r"lowering.{0,40}guidance|guidance.{0,30}below", blob, re.I))
    raised = bool(re.search(r"raising.{0,40}guidance|increased.{0,30}guidance", blob, re.I))
    return {"lo": lo, "hi": hi, "cut": cut, "raised": raised}


def body(stamp: str, cfg: dict, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | CEG | {cfg['display']}",
            f"이게 뭐 알림: {cfg['explain']}",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def zone_of(price: float, upgrade: float, stretch: float) -> str:
    if price <= upgrade:
        return "upgrade"
    if price >= stretch:
        return "stretch"
    return "mid"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"dry", "test", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(
        STATE_PATH,
        {
            "price": None,
            "zone": None,
            "upgrade_sent": False,
            "stretch_sent": False,
            "earn_sent": False,
            "fail_sent_date": None,
            "updated_at": None,
        },
    )
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    year_ok = now.year == int(cfg["year_guard"])
    valid = today <= cfg["valid_until"]
    print(f"{stamp} mode={mode} year_ok={year_ok} valid={valid}")
    if mode not in {"force", "dry", "test", "dry-run", "earn"} and (not year_ok or not valid):
        print("outside 2026 window; silent")
        return 0
    if mode == "earn" and not year_ok:
        print("earn outside year; silent")
        return 0

    price, qerr = quote(cfg["sources"]["quote"])
    upgrade = float(cfg["levels"]["upgrade"])
    stretch = float(cfg["levels"]["stretch"])
    zone = zone_of(price, upgrade, stretch) if price is not None else "NO_PX"
    print(f"price={price} zone={zone} qerr={qerr}")

    alerts = []
    guide = cfg["guide_2026"]
    later = (
        f"실적 창은 {', '.join(cfg['oneshot_kst'])} KST. {cfg['earnings_note']} "
        "Crane 지연, 하이퍼스케일러 계약 취소, PJM 용량가격 급락은 이 알림이 보지 않습니다."
    )

    if price is None:
        if state.get("fail_sent_date") != today or mode in {"force", "dry", "test"}:
            alerts.append(
                (
                    "[CEG] 시세 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        cfg,
                        f"가격 조회 실패. {qerr or '빈 응답'}. 조건 미충족이 아닙니다.",
                        "증권 앱에서 CEG 종가를 직접 보세요. 240달러, 348달러를 이 알림으로 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 사지 마세요.",
                        "조회 실패를 240달러 진입이나 매수 신호로 보기.",
                    ),
                    4,
                    "FAIL",
                )
            )
    else:
        prev = state.get("zone")
        if zone == "upgrade" and (mode in {"force", "dry", "test"} or not state.get("upgrade_sent")):
            alerts.append(
                (
                    "[CEG] 240달러 이하 | 매력 상향 검토",
                    body(
                        stamp,
                        cfg,
                        f"CEG {price:.2f}달러. 기준 {cfg['ref_date']} {cfg['ref_price']}달러. 가이던스 중값 12달러의 20배인 240달러 이하.",
                        "분할 관심만. 전액 금지. 2026 조정 EPS 하단 11.50달러 유지 여부를 실적에서 확인하세요.",
                        later,
                        "가이던스 하단이 11.50 미만이면 이 가격도 매력 상향이 아닙니다.",
                        "240달러 터치를 매수 확정으로 보기. 신용, 미수, 추격.",
                    ),
                    4,
                    "UPGRADE",
                )
            )
        elif zone == "stretch" and (mode in {"force", "dry", "test"} or not state.get("stretch_sent")):
            alerts.append(
                (
                    "[CEG] 348달러 이상 | 추격 금지",
                    body(
                        stamp,
                        cfg,
                        f"CEG {price:.2f}달러. 컨센서스 평균 목표 근처(348달러) 이상. 기준가 {cfg['ref_price']}달러.",
                        "새 돈으로 따라가지 마세요. 중간 판단이 더 나빠집니다.",
                        later,
                        "348달러 위는 추격 금지. 보유 물량 손절 신호는 아닙니다.",
                        "목표 도달을 매수 신호로 보기. 신용, 미수.",
                    ),
                    3,
                    "STRETCH",
                )
            )
        else:
            print(f"price zone {zone} (prev {prev}); no price alert")

    if mode == "earn" or mode in {"force", "dry", "test"}:
        found = False
        parsed = {"lo": None, "hi": None, "cut": False, "raised": False}
        source = ""
        err = None
        try:
            day, link = latest_8k(cfg["sources"]["sec"])
            ir = visible(fetch(cfg["sources"]["ir"]))
            blob = ir
            if link:
                try:
                    blob = visible(fetch(link)) + " " + ir
                    source = link
                except Exception as exc:
                    print(f"8-K body fail: {exc}")
                    source = cfg["sources"]["ir"]
            else:
                source = cfg["sources"]["ir"]
            parsed = parse_guide(blob)
            found = parsed["lo"] is not None or "third quarter 2026" in blob.lower() or "q3 2026" in blob.lower()
            print(f"filing={day} found={found} parsed={parsed}")
        except Exception as exc:
            err = str(exc)
            print(f"earn fetch fail: {exc}")
        if err and (state.get("fail_sent_date") != today or mode in {"force", "dry", "test"}):
            alerts.append(
                (
                    "[CEG] 실적 조회 실패 | 가이던스 미확인",
                    body(
                        stamp,
                        cfg,
                        f"실적 원문 조회 실패. {err}. 가이던스 미확인이며 조건 미충족이 아닙니다.",
                        "investors.constellationenergy.com 실적 보도문을 직접 보세요.",
                        later,
                        "이번 알림으로 비중을 바꾸지 마세요.",
                        "조회 실패를 가이던스 유지로 보기.",
                    ),
                    4,
                    "EARN_FAIL",
                )
            )
        elif not found and not state.get("earn_sent"):
            alerts.append(
                (
                    "[CEG] 실적 미게시 | 가이던스 미확인",
                    body(
                        stamp,
                        cfg,
                        f"이 시각에 2026년 3분기 가이던스 숫자를 확인하지 못함. 출처 {source or 'IR'}.",
                        "원문이 나올 때까지 240달러 조건을 켜지 마세요. 미게시는 호재가 아닙니다.",
                        later,
                        "숫자 없이 추격하거나 줄이지 마세요.",
                        "미게시를 가이던스 유지로 단정.",
                    ),
                    3,
                    "UNPOSTED",
                )
            )
        elif found and (mode in {"force", "dry", "test"} or not state.get("earn_sent")):
            lo, hi = parsed.get("lo"), parsed.get("hi")
            base_lo = float(guide["lo"])
            if lo is not None and lo < base_lo:
                title = "[CEG] 가이던스 하단 이탈 | 매력 하락"
                action = f"조정 EPS 하단 {lo:.2f}달러가 기준 {base_lo:.2f} 미만. 240달러도 매력 상향이 아닙니다."
                kind = "EARN_DOWN"
                pri = 5
            elif lo is not None and lo > base_lo:
                title = "[CEG] 가이던스 하단 상향 | 추격은 금지"
                action = f"하단 {lo:.2f}달러로 기준 {base_lo:.2f}보다 높음. 가격이 240달러 초과면 분할도 보류."
                kind = "EARN_UP"
                pri = 4
            elif parsed.get("cut"):
                title = "[CEG] 가이던스 하향 문구 | 매력 하락"
                action = "하향 문구가 있습니다. 숫자 파싱이 없으면 원문을 확인하세요. 추가 관심 보류."
                kind = "EARN_DOWN"
                pri = 5
            else:
                title = "[CEG] 가이던스 유지 | 중간 판단 유지"
                action = "하단 11.50 이탈은 확인되지 않음. 300달러대 추격 금지, 240달러 이하만 재검토."
                kind = "EARN_HOLD"
                pri = 3
            num = f"파싱 하단 {lo} 상단 {hi}" if lo is not None else "가이던스 범위 파싱 실패"
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        cfg,
                        f"실적 관련 문구 확인. {num}. 현재가 {price if price is None else f'{price:.2f}달러'}. 출처 {source}.",
                        action,
                        later,
                        "하단 11.50 미만이면 관심 종료. 상향이어도 348달러 추격 금지.",
                        "파싱 실패를 상향으로 세기. 신용, 미수.",
                    ),
                    pri,
                    kind,
                )
            )

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri, kind in alerts:
        print(title)
        print(msg)
        print(f"kind={kind} dry={dry}")
        if dry:
            continue
        publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
        if kind == "UPGRADE":
            state["upgrade_sent"] = True
        elif kind == "STRETCH":
            state["stretch_sent"] = True
        elif kind in {"EARN_DOWN", "EARN_UP", "EARN_HOLD"}:
            state["earn_sent"] = True
        elif kind in {"FAIL", "EARN_FAIL"}:
            state["fail_sent_date"] = today
        elif kind == "UNPOSTED":
            state["earn_sent"] = False
    if price is not None:
        state["price"] = price
        state["zone"] = zone
    if not dry:
        save_state(state, now)
    if not alerts:
        print("no alert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
