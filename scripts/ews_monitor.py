#!/usr/bin/env python3
"""EWS change-condition watch. Silent unless a tracked flag flips. Not a buy order."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "ews.json"
STATE_PATH = ROOT / "state" / "ews.json"
UA = {"User-Agent": "cwally-alerts ews research monitor research@example.com"}


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


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def quote(url: str) -> tuple[float | None, float | None, str | None]:
    raw = fetch(url)
    data = json.loads(raw)
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, None, "quote empty"
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    pe = meta.get("trailingPE")
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    try:
        pe_f = float(pe) if pe is not None else None
    except (TypeError, ValueError):
        pe_f = None
    return price_f, pe_f, None


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | EWS | 변경조건 점검",
            "이게 뭐 알림: 싱가포르 ETF(EWS) 관망 판단의 변경 조건만 보는 알림입니다. 매수 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def mas_update(html: str, last_known: str) -> tuple[str | None, str | None]:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    dates = re.findall(r"(\d{1,2}\s+[A-Z][a-z]{2}\s+2026)", text)
    parsed = []
    for raw in dates:
        try:
            parsed.append(datetime.strptime(raw, "%d %b %Y").date())
        except ValueError:
            continue
    if not parsed:
        return None, None
    newest = max(parsed)
    known = datetime.strptime(last_known, "%Y-%m-%d").date()
    if newest <= known:
        return None, None
    window = text
    candidates = [
        newest.strftime("%d %b %Y").lstrip("0"),
        newest.strftime("%d %b %Y"),
    ]
    idx = -1
    for cand in candidates:
        idx = text.find(cand)
        if idx >= 0:
            break
    if idx < 0:
        idx = text.lower().find(newest.strftime("%b %Y").lower())
    if idx >= 0:
        window = text[max(0, idx - 80) : idx + 220]
    low = window.lower()
    if "reduce" in low or "decrease" in low:
        stance = "ease"
    elif "increase" in low:
        stance = "tighten"
    elif "unchanged" in low or "maintain" in low:
        stance = "hold"
    else:
        stance = "unread"
    return newest.isoformat(), stance


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("EWS_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "drawdown_on": False,
            "pe_on": False,
            "mas_last": cfg.get("mas_last_known"),
            "mas_stance": None,
            "fetch_fail_date": None,
        },
    )
    now = datetime.now(KST)
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    th = cfg["thresholds"]
    base = cfg["baseline"]
    ref_high = float(base["ref_high"])
    price, pe, qerr = None, None, None
    try:
        price, pe, qerr = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    print(f"price={price} pe={pe} qerr={qerr}")

    alerts: list[tuple[str, str, int]] = []
    if price is None:
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append(
                (
                    "[EWS] 주가 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        f"EWS 시세 조회 실패. {qerr or '빈 응답'}. 조건 미충족이 아닙니다.",
                        "야후나 증권 앱에서 EWS 종가를 직접 보세요. 고점 대비 10%와 P/E 16을 이 알림으로 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 비중을 바꾸지 마세요.",
                        "조회 실패를 조정 진입이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        dd = (ref_high - price) / ref_high * 100.0
        on = bool(state.get("drawdown_on"))
        hit = dd >= float(th["drawdown_pct"])
        clear = dd <= float(th["drawdown_clear_pct"])
        if hit and not on:
            level = ref_high * (1 - float(th["drawdown_pct"]) / 100.0)
            alerts.append(
                (
                    "[EWS] 고점 대비 10% 조정 | 분할 관심",
                    body(
                        stamp,
                        f"EWS {price:.2f}달러. 기준 고점 {ref_high:.2f}달러({base['ref_high_source']}) 대비 {dd:.1f}% 하락. 10% 선 {level:.2f}달러. 은행 건전성은 미확인.",
                        "분할 관심만 켜세요. 은행 대손, NIM, CET1을 공시에서 보기 전 신규 비중을 확정하지 마세요.",
                        "P/E 16 부근과 MAS 완화는 별개 조건입니다. 둘 다 켜져야 매력이 커집니다.",
                        "고점 대비 낙폭이 8% 안으로 줄면 이 관심은 해제됩니다. 보유 손절 신호는 아닙니다.",
                        "10% 조정을 매수 확정으로 쓰기. 신용, 미수, 추격.",
                    ),
                    4,
                )
            )
            state["drawdown_on"] = True
        elif on and clear:
            alerts.append(
                (
                    "[EWS] 10% 조정 해제 | 관망 복귀",
                    body(
                        stamp,
                        f"EWS {price:.2f}달러. 고점 {ref_high:.2f}달러 대비 {dd:.1f}%. 8% 해제선 안입니다.",
                        "가격 관심만 끄세요. 관망 유지.",
                        "다시 10% 이상 벌어지면 재알림합니다.",
                        "이번 해제로 비중을 늘리지 마세요.",
                        "해제를 실적 개선으로 단정.",
                    ),
                    3,
                )
            )
            state["drawdown_on"] = False
        else:
            print(f"drawdown {dd:.2f}% unchanged on={on}")
        state["last_price"] = price
        state["last_drawdown_pct"] = round(dd, 2)

        if pe is None:
            print("pe unavailable; not a signal")
        else:
            pe_on = bool(state.get("pe_on"))
            if pe <= float(th["pe_zone"]) and not pe_on:
                alerts.append(
                    (
                        "[EWS] P/E 16 부근 | 밸류 조건",
                        body(
                            stamp,
                            f"야후 trailing P/E {pe:.2f}. 기준일 {base['pe_date']} iShares P/E {base['pe']}. 구간 16.5 이하. MAS 완화 여부는 별도.",
                            "밸류 조건만 켜졌습니다. MAS가 완화로 바뀌기 전 추격하지 마세요.",
                            "P/E가 17.5 위로 올라가면 이 조건은 해제됩니다.",
                            "이번 알림만으로 사지 마세요.",
                            "야후 P/E를 iShares 공식 P/E와 같은 숫자로 단정. 신용, 미수.",
                        ),
                        4,
                    )
                )
                state["pe_on"] = True
            elif pe_on and pe >= float(th["pe_clear"]):
                alerts.append(
                    (
                        "[EWS] P/E 구간 해제 | 관망",
                        body(
                            stamp,
                            f"야후 trailing P/E {pe:.2f}. 해제선 17.5 이상.",
                            "밸류 조건만 끄세요.",
                            "가격 10% 조건은 따로 보세요.",
                            "이번 해제로 비중을 늘리지 마세요.",
                            "P/E 해제를 고평가 확정으로 단정.",
                        ),
                        3,
                    )
                )
                state["pe_on"] = False
            else:
                print(f"pe {pe:.2f} unchanged on={pe_on}")
            state["last_pe"] = pe

    mas_err = None
    try:
        mas_html = fetch(cfg["sources"]["mas"])
        new_day, stance = mas_update(mas_html, state.get("mas_last") or cfg["mas_last_known"])
    except Exception as exc:
        mas_err = str(exc)
        new_day, stance = None, None
        print(f"mas fail: {exc}")
    if new_day:
        if stance == "ease":
            alerts.append(
                (
                    "[EWS] MAS 완화 문구 | 변경 조건",
                    body(
                        stamp,
                        f"MAS 결정 목록에 {new_day} 신규, 기울기 축소 문구. 직전 확인일 {state.get('mas_last')}. P/E 16 여부는 별도.",
                        "원문에서 기울기 축소인지 확인한 뒤, P/E가 16 부근일 때만 분할 관심을 보세요.",
                        "은행 건전성과 물가는 이 알림이 판정하지 않습니다.",
                        "완화 단독으로 비중을 늘리지 마세요.",
                        "목록 문구를 성명 전문 확정으로 단정.",
                    ),
                    4,
                )
            )
        elif stance == "tighten":
            alerts.append(
                (
                    "[EWS] MAS 추가 긴축 문구 | 중립 유지",
                    body(
                        stamp,
                        f"MAS 결정 목록에 {new_day} 신규, 기울기 확대 문구. 직전 확인일 {state.get('mas_last')}. 긴축 단독은 중립 철회가 아닙니다.",
                        "관망 유지. 근원물가 2.5% 상회와 AI 수출 둔화는 직접 확인하세요. 이 알림은 그 둘을 판정하지 않습니다.",
                        "물가와 수출이 동시에 나빠지면 그때 중립 철회를 다시 보세요.",
                        "이번 알림으로 비중을 줄이거나 늘리지 마세요.",
                        "긴축 한 건을 중립 철회로 단정.",
                    ),
                    3,
                )
            )
        elif stance == "hold":
            alerts.append(
                (
                    "[EWS] MAS 동결 문구 | 완화 아님",
                    body(
                        stamp,
                        f"MAS 결정 목록에 {new_day} 신규, 유지 문구. 완화 조건은 꺼진 상태입니다.",
                        "관망 유지. 원문만 대조하세요.",
                        "다음 성명 전엔 이 판정을 유지.",
                        "동결을 완화로 읽지 마세요.",
                        "동결을 매수 신호로 쓰기.",
                    ),
                    3,
                )
            )
        else:
            alerts.append(
                (
                    "[EWS] MAS 문구 해석 불가 | 조건 미확인",
                    body(
                        stamp,
                        f"MAS 목록에 {new_day} 신규는 보였으나 확대/축소/유지 문구를 못 읽었습니다.",
                        "mas.gov.sg 성명 원문을 직접 보세요. 완화나 긴축으로 세지 마세요.",
                        "원문 확인 전 이 조건을 켜지 마세요.",
                        "이번 알림으로 비중을 바꾸지 마세요.",
                        "해석 불가를 완화 충족으로 세기.",
                    ),
                    3,
                )
            )
        state["mas_last"] = new_day
        state["mas_stance"] = stance
    elif mas_err and state.get("fetch_fail_date") != today.isoformat() and today.weekday() == 0:
        alerts.append(
            (
                "[EWS] MAS 조회 실패 | 정책 조건 미확인",
                body(
                    stamp,
                    f"MAS 결정 목록 조회 실패. {mas_err}. 정책 조건은 미확인입니다.",
                    "mas.gov.sg에서 10월 성명(공표 상한 2026-10-14)을 직접 보세요.",
                    "다음 월요일에 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 동결이나 완화로 취급.",
                ),
                4,
            )
        )
        state["fetch_fail_date"] = today.isoformat()

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")

    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
