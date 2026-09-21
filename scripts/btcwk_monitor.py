#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "btcwk.json"
STATE_PATH = ROOT / "state" / "btcwk.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch_json(url: str):
    import urllib.request

    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def load_state() -> dict:
    state = {
        "final_notified": False,
        "fail_notified": False,
        "verdict": None,
        "updated_at": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def completed_weeks(rows: list) -> list[dict]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    out = []
    for row in rows:
        close_px, close_ms = float(row[4]), int(row[6])
        if close_ms > now_ms:
            continue
        out.append({"close": close_px, "close_ms": close_ms, "low": float(row[3])})
    return out


def classify(weekly_close: float, sma50: float, cfg: dict) -> str:
    if weekly_close < float(cfg["june_low"]):
        return "F"
    if weekly_close < float(cfg["sma200_approx"]):
        return "E"
    if weekly_close < sma50:
        return "D"
    if weekly_close >= 95000:
        return "G"
    if weekly_close >= float(cfg["level_83k"]):
        return "C"
    return "A"


def copy_for(kind, cfg, weekly_close, sma50, spot, week_low):
    wc = f"{weekly_close:,.0f}달러" if weekly_close is not None else "없음"
    sm = f"{sma50:,.0f}달러" if sma50 is not None else "없음"
    sp = f"{spot:,.0f}달러" if spot is not None else "없음"
    vs83 = "위" if weekly_close is not None and weekly_close >= cfg["level_83k"] else "아래"
    vssma = "위" if weekly_close is not None and sma50 is not None and weekly_close >= sma50 else "아래"
    titles = {
        "A": "[BTCWK] 주간 8.3만 미달 | 추격 보류",
        "B": "[BTCWK] 평균선 위 되돌림 | 분할 검토",
        "C": "[BTCWK] 주간 8.3만 충족 | 되돌림만 분할",
        "D": "[BTCWK] 주간 평균선 이탈 | 신규 중단",
        "E": "[BTCWK] 주간 6.5만 이탈 | 전술 축소",
        "F": "[BTCWK] 6월 저점 이탈 | 가설 폐기",
        "G": "[BTCWK] 9.5만 이상 급등 | 전술 일부 익절",
        "FETCH_FAIL": "[BTCWK] 조회 실패 | 판정 보류",
    }
    now_do = {
        "A": "주간 종가가 8.3만 아래입니다. 지금 8.5만대를 붔아 사지 마세요. 다음 자리는 7.9만~8.1만 되돌림입니다.",
        "B": "평균선은 지켰고 가격이 내려온 자리입니다. 계획 금액의 20~30%만 분할을 검토하세요.",
        "C": "주간 8.3만은 충족입니다. 추격은 금지. 다음 주에도 50주 평균선 위에서 막히면 계획의 20%를 더 볼 수 있습니다.",
        "D": "주간 종가가 50주 평균선 아래입니다. 신규 매수를 멈추세요. 최근 분할분은 축소를 검토하세요.",
        "E": "주간 종가가 6.5만 아래입니다. 10월 저점 시나리오를 다시 켝니다. 전술 포지션을 줄이세요.",
        "F": "6월 저점 5.85만을 주간으로 깨슶습니다. 남은 전술 분은 손절 또는 대폭 축소하세요.",
        "G": "주간 종가가 9.5만 이상입니다. 전술 분의 20~30% 익절을 검토하세요. 전량은 유지해도 됩니다.",
        "FETCH_FAIL": "시세를 못 읽었습니다. 실패를 정상으로 보지 마세요. 차트에서 주간 종가만 직접 확인하세요.",
    }
    later = {
        "A": "나중에: 다음 주 주간 종가가 8.3만과 평균선을 다시 가르면 그때 판정을 바꿉니다.",
        "B": "나중에: 주간 종가가 평균선 아래로 닫히면 D로 바뀱니다.",
        "C": "나중에: 다음 주 평균선 아래 마감이면 이번 확인은 취소입니다.",
        "D": "나중에: 바로 다음 주 평균선을 다시 넘으면 재탈환으로 밥니다.",
        "E": "나중에: 6.5만 주간 재탈환 전에는 저점 탐색입니다.",
        "F": "나중에: 이 가설은 끝입니다. 새 저점 기준으로 다시 짜세요.",
        "G": "나중에: 주간 8.3만 위에 붙어 있으면 전량은 유지.",
        "FETCH_FAIL": "나중에: 이 작업은 오늘 한 번입니다. 실패면 직접 확인하세요.",
    }
    sell = {
        "A": "익절/줄일 때: 이 알림으로 팔지 마세요.",
        "B": "익절/줄일 때: 주간 평균선 이탈이면 이번 분할을 되돌리세요.",
        "C": "익절/줄일 때: 9.5만~10만 급등이면 전술 20~30%만.",
        "D": "익절/줄일 때: 전술 축소. 5.85만은 가설 폐기 선입니다.",
        "E": "익절/줄일 때: 전술 축소가 우선입니다.",
        "F": "익절/줄일 때: 전술 손절/대폭 축소.",
        "G": "익절/줄일 때: 전술 20~30% 익절. 전량 청산 신호는 아닙니다.",
        "FETCH_FAIL": "익절/줄일 때: 이 알림으로 팔지 마세요.",
    }
    status = (
        f"지금 결과: 시나리오 {kind} | 주간종가 {wc} (8.3만 {vs83}, 50주평균 {sm} {vssma}) | 현재가 {sp} | 주간저점 {week_low:,.0f}달러"
        if weekly_close is not None and sma50 is not None and week_low is not None
        else f"지금 결과: 시나리오 {kind} | 주간종가 {wc} | 50주평균 {sm} | 현재가 {sp}"
    )
    return (
        titles[kind],
        [
            f"이게 뭐 알림: {cfg['explain']}",
            status,
            f"지금 할 일: {now_do[kind]}",
            later[kind],
            sell[kind],
            "하지 말 것: 장중 고점을 주간 종가로 보기. 추격매수. 신용/미수. 레버리지.",
        ],
        5 if kind in {"D", "E", "F"} else 4,
    )


def main() -> int:
    dry = len(sys.argv) > 1 and sys.argv[1].lower() in {"test", "dry", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    if now.year != int(cfg.get("event_year", 2026)):
        print("year-guard skip")
        return 0
    if today < cfg["window_start_kst"][:10] or today > cfg["expire_date"]:
        print("outside window; silent")
        return 0
    if state.get("final_notified"):
        print("already sent final; skip")
        return 0

    weekly_close = sma50 = spot = week_low = None
    kind = "FETCH_FAIL"
    note = ""
    try:
        weeks = completed_weeks(fetch_json(cfg["sources"]["weekly"]))
        if len(weeks) < 50:
            raise RuntimeError(f"completed weeks {len(weeks)}")
        last = weeks[-1]
        weekly_close = last["close"]
        week_low = last["low"]
        sma50 = mean(w["close"] for w in weeks[-50:])
        close_kst = datetime.fromtimestamp(last["close_ms"] / 1000, KST)
        note = f"주간캔들 마감 {close_kst.strftime('%Y-%m-%d %H:%M KST')}"
        kind = classify(weekly_close, sma50, cfg)
        try:
            spot = float(fetch_json(cfg["sources"]["spot"])["price"])
        except Exception as exc:
            note += f" / 현재가 실패: {exc}"
        if kind == "A" and spot is not None and sma50 <= spot <= 81000 and weekly_close >= sma50:
            kind = "B"
    except Exception as exc:
        kind = "FETCH_FAIL"
        note = f"조회 실패: {exc}"

    title, body, pri = copy_for(kind, cfg, weekly_close, sma50, spot, week_low)
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | BTCWK | 주간 8.3만/50주평균", *body]
    if note:
        lines.append(f"메모: {note}")
    lines.append("투자 권유가 아닙니다.")
    msg = "\n".join(lines)
    print(msg)
    print(f"verdict={kind} dry={dry}")

    send = False
    if kind == "FETCH_FAIL":
        send = not state.get("fail_notified")
        if send and not dry:
            state["fail_notified"] = True
    else:
        send = True
        if not dry:
            state["final_notified"] = True

    if dry:
        print("dry-run; no ntfy")
    elif send:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
    else:
        print("no ntfy; duplicate suppressed")

    state.update({"verdict": kind, "weekly_close": weekly_close, "sma50": sma50, "spot": spot, "updated_at": now.isoformat()})
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
