#!/usr/bin/env python3
"""한국정보인증(053300) watch.

  kica_monitor.py            daily (flow slot 15:40): silent unless a condition hits
  kica_monitor.py daily dry  same, prints only (no ntfy, no state write)
  kica_monitor.py auto       Q3 2026 earnings one-shot (oneshot_pick passes "auto")
  kica_monitor.py dry        earnings preview + daily preview, no ntfy, no state write
  kica_monitor.py force      earnings one-shot ignoring the date window
  kica_monitor.py install    one install-confirmation send (AGENTS.md), no state write

Daily conditions: KRX close moves +-move_pct vs the previous KRX close, or closes above band_high /
below band_low (re-arms once the close is band_reset_margin back inside). Not a trade order.
"""
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
CFG_PATH = ROOT / "configs" / "kica.json"
STATE_PATH = ROOT / "state" / "kica.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
DEFAULT_STATE = {
    "updated_at": None,
    "fired": {"band_high": False, "band_low": False},
    "last_move_day": None,
    "last_fail_day": None,
    "last_bar": None,
    "last_sent": None,
    "earnings_notified": False,
    "empty_notified": False,
}


def load_json(path: Path, fallback: dict) -> dict:
    out = json.loads(json.dumps(fallback))
    if not path.exists():
        return out
    try:
        out.update(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        pass
    return out


def save(state: dict, dry: bool) -> None:
    if dry:
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def send(cfg: dict, title: str, body: str, pri: int, dry: bool) -> None:
    print(f"TITLE: {title}\n{body}")
    if dry:
        print("DRY RUN: no ntfy sent")
        return
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title[:180],
        body,
        pri,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )


def fail_once(state: dict, cfg: dict, now: datetime, note: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    if state.get("last_fail_day") == day and not dry:
        print("fail already notified today")
        return
    body = "\n".join(
        [
            f"{now.strftime('%Y-%m-%d %H:%M KST')} | 053300",
            "이게 뭐 알림: 한국정보인증 점검 중 조회가 실패했습니다.",
            f"지금 결과/상태: {note}",
            "지금 할 일: 실패를 가격 조건 충족으로 보지 마세요. 종가는 직접 확인하세요.",
            "나중에: 다음 평일 15:40 KST에 다시 봅니다.",
            "익절/줄일 때: 이 실패로 사거나 팔지 마세요.",
            "하지 말 것: 조회 실패를 급등락 신호로 해석.",
        ]
    )
    send(cfg, "[KICA] 시세 조회 실패 | 판정 보류", body, 4, dry)
    if not dry:
        state["last_fail_day"] = day


def krx_bars(cfg: dict, now: datetime) -> list[dict]:
    url = cfg["sources"]["daily"].format(
        start=(now - timedelta(days=20)).strftime("%Y%m%d"), end=now.strftime("%Y%m%d")
    )
    text = fetch(url)
    rows = re.findall(r'\["(\d{8})",\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)', text)
    bars = [
        {"day": f"{d[:4]}-{d[4:6]}-{d[6:]}", "close": int(c), "volume": int(v)}
        for d, o, h, lo, c, v in rows
    ]
    return sorted(bars, key=lambda b: b["day"], reverse=True)


def valuation(cfg: dict) -> str:
    try:
        j = json.loads(fetch(cfg["sources"]["integration"]))
        m = {x.get("code"): x.get("value") for x in j.get("totalInfos", [])}
        return f"PER {m.get('per')}, PBR {m.get('pbr')}, 배당수익률 {m.get('dividendYieldRatio')}"
    except Exception as exc:  # valuation is nice-to-have; never block the alert
        return f"밸류 조회 실패({exc.__class__.__name__})"


def daily(cfg: dict, state: dict, now: datetime, dry: bool) -> None:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg.get("year_guard") or 2026):
        print("kica year-guard silent")
        return
    if now.weekday() >= 5:
        print("kica weekend silent")
        return
    if now.strftime("%H:%M") < "15:35" and not dry:
        print("kica before KRX close; silent")
        return
    try:
        bars = krx_bars(cfg, now)
    except Exception as exc:
        fail_once(state, cfg, now, f"한국정보인증 시세 조회 실패: {exc}", dry)
        save(state, dry)
        return
    if len(bars) < 2:
        print("kica not enough bars; silent")
        return
    cur, prev = bars[0], bars[1]
    day, close = cur["day"], cur["close"]
    lv = cfg["levels"]
    lo, hi = float(lv["band_low"]), float(lv["band_high"])
    mv, margin = float(lv["move_pct"]), float(lv.get("band_reset_margin", 100))
    chg = (close - prev["close"]) / prev["close"] * 100.0
    fired = dict(state.get("fired") or {})
    fired.setdefault("band_high", False)
    fired.setdefault("band_low", False)
    if close <= hi - margin:
        fired["band_high"] = False
    if close >= lo + margin:
        fired["band_low"] = False
    print(stamp, "bar", day, "close", close, f"chg {chg:+.2f}%", "prev", prev["close"])
    base_day = str((cfg.get("baseline") or {}).get("day") or "")
    state["last_bar"] = {"day": day, "close": close, "chg_pct": round(chg, 2)}
    state["updated_at"] = stamp
    if day <= base_day:
        print("kica baseline day or earlier; silent")
        state["fired"] = fired
        save(state, dry)
        return
    hits = []
    if abs(chg) >= mv and state.get("last_move_day") != day:
        hits.append("move_up" if chg > 0 else "move_down")
    if close > hi and not fired["band_high"]:
        hits.append("band_high")
    if close < lo and not fired["band_low"]:
        hits.append("band_low")
    if not hits:
        state["fired"] = fired
        save(state, dry)
        print("kica silent; no condition")
        return
    parts = []
    if "move_up" in hits:
        parts.append(f"하루 {chg:+.1f}% 상승")
    if "move_down" in hits:
        parts.append(f"하루 {chg:+.1f}% 하락")
    if "band_high" in hits:
        parts.append(f"{hi:,.0f}원 상단 돌파")
    if "band_low" in hits:
        parts.append(f"{lo:,.0f}원 하단 이탈")
    title = f"[KICA] {close:,}원 {chg:+.1f}% | " + ", ".join(parts)
    if "move_down" in hits or "band_low" in hits:
        action = "크게 빠졌습니다. 서두르지 말고 이유(공시, 뉴스)를 먼저 확인하세요."
    else:
        action = "크게 올랐습니다. 이유(공시, 뉴스)를 먼저 확인하세요."
    body = "\n".join(
        [
            f"{stamp} | 053300",
            "이게 뭐 알림: 한국정보인증 종가가 정해둔 조건(하루 5% 이상 변동, 4,500~6,500원 밖)에 걸렸습니다. 매매 신호가 아닙니다.",
            f"지금 결과/상태: {day} 종가 {close:,}원 (전일 {prev['close']:,}원, {chg:+.2f}%). {valuation(cfg)}.",
            f"지금 할 일: {action}",
            "나중에: 3분기 분기보고서(11월 13일 전후 예상)에서 영업이익을 확인합니다. 밴드 알림은 종가가 안쪽으로 100원 돌아오면 다시 열립니다.",
            "익절/줄일 때: 이 알림은 주문이 아닙니다. 줄일지는 본인이 미리 정한 기준으로 판단하세요.",
            "하지 말 것: 하루 움직임만 보고 추격매수. 신용/미수.",
        ]
    )
    send(cfg, title, body, 4, dry)
    if not dry:
        if "move_up" in hits or "move_down" in hits:
            state["last_move_day"] = day
        if "band_high" in hits:
            fired["band_high"] = True
        if "band_low" in hits:
            fired["band_low"] = True
        state["last_sent"] = {"at": stamp, "hits": hits}
    state["fired"] = fired
    save(state, dry)


def num(v) -> float | None:
    s = str(v if v is not None else "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def quarter(cfg: dict) -> dict | None:
    key = cfg["earnings"]["key"]
    fi = json.loads(fetch(cfg["sources"]["quarter"]))["financeInfo"]
    cons = {t["key"]: t.get("isConsensus") == "Y" for t in fi["trTitleList"]}
    if key not in cons or cons[key]:
        return None  # not reported yet (consensus/placeholder column)
    rows = {r["title"]: (r["columns"].get(key) or {}).get("value") for r in fi["rowList"]}
    op = num(rows.get("영업이익"))
    if op is None:
        return None
    return {"op": op, "rev": num(rows.get("매출액")), "ni": num(rows.get("당기순이익"))}


def earnings(cfg: dict, state: dict, now: datetime, mode: str, dry: bool) -> None:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    e = cfg["earnings"]
    expire = datetime.fromisoformat(e["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    if mode == "auto" and (now.year != int(cfg.get("year_guard") or 2026) or now > expire):
        print("kica earnings outside window; silent")
        return
    if state.get("earnings_notified") and mode != "force":
        print("kica earnings already notified; silent")
        return
    try:
        q = quarter(cfg)
    except Exception as exc:
        fail_once(state, cfg, now, f"3분기 실적 조회 실패: {exc}", dry)
        save(state, dry)
        return
    if q is None:
        if state.get("empty_notified") and not dry:
            print("kica earnings not published; already told")
            return
        body = "\n".join(
            [
                f"{stamp} | 053300",
                "이게 뭐 알림: 한국정보인증 3분기 실적을 확인하려 했는데 아직 게시되지 않았습니다.",
                "지금 결과/상태: 네이버 분기 실적에 2026.09 확정치가 없습니다. 공시가 아직 없거나 반영이 늦은 것입니다.",
                "지금 할 일: DART에서 한국정보인증 분기보고서가 올라왔는지 직접 확인하세요.",
                "나중에: 법정 제출 기한은 11월 14일(토)이라 실제로는 11월 16일(월)까지 밀릴 수 있습니다. 자동 재확인은 없습니다.",
                "익절/줄일 때: 실적 숫자를 확인하기 전에는 판단하지 마세요.",
                "하지 말 것: 미게시를 호재나 악재로 해석.",
            ]
        )
        send(cfg, "[KICA] 3분기 실적 미게시 | 직접 확인", body, 3, dry)
        if not dry:
            state["empty_notified"] = True
        save(state, dry)
        return
    op, rev, ni = q["op"], q["rev"], q["ni"]
    prev_q3, q2, h1 = float(e["prev_q3_op_eok"]), float(e["q2_op_eok"]), float(e["h1_op_eok"])
    yoy = (op - prev_q3) / prev_q3 * 100.0
    qoq = (op - q2) / q2 * 100.0
    if yoy >= 10:
        tone = "작년 3분기보다 늘었습니다"
    elif yoy <= -10:
        tone = "작년 3분기보다 줄었습니다"
    else:
        tone = "작년 3분기와 비슷합니다"
    title = f"[KICA] 3분기 영업이익 {op:,.0f}억 | 전년비 {yoy:+.0f}%, 관찰"
    rev_txt = f"매출 {rev:,.0f}억, " if rev is not None else ""
    ni_txt = f", 순이익 {ni:,.0f}억" if ni is not None else ""
    body = "\n".join(
        [
            f"{stamp} | 053300",
            "이게 뭐 알림: 한국정보인증 2026년 3분기 실적(연결) 결과입니다. 매매 신호가 아닙니다.",
            f"지금 결과/상태: {rev_txt}영업이익 {op:,.0f}억{ni_txt}. 전년 3분기 {prev_q3:,.0f}억 대비 {yoy:+.0f}%, 2분기 {q2:,.0f}억 대비 {qoq:+.0f}%. {tone}. 1~3분기 누적 영업이익은 약 {h1 + op:,.0f}억(상반기 {h1:,.0f}억 기준).",
            "지금 할 일: 다음 거래일 시가 반응을 보고 분기보고서(DART)에서 광고대행 자회사와 인증 사업 비중을 확인하세요.",
            "나중에: 4분기 실적과 배당 정책(2025년 주당 200원)은 내년 초에 나옵니다.",
            "익절/줄일 때: 이 알림은 주문이 아닙니다. 실적이 좋아도 줄일 기준은 본인이 미리 정한 값으로 판단하세요.",
            "하지 말 것: 한 분기 숫자만 보고 추격매수. 신용/미수.",
        ]
    )
    send(cfg, title, body, 4, dry)
    if not dry:
        state["earnings_notified"] = True
        state["last_sent"] = {"at": stamp, "hits": ["earnings"], "op": op}
    save(state, dry)


def install(cfg: dict) -> None:
    body = "\n".join(
        [
            "이게 뭐 알림: 한국정보인증(053300) 알림이 설치됐다는 확인입니다.",
            "지금 결과/상태: 평일 15:40 종가 점검(하루 5% 이상 변동 또는 4,500~6,500원 이탈 때만 발송)과 11월 13일 18:00 3분기 실적 확인이 켜졌습니다.",
            "지금 할 일: 없음. 매매 신호가 아닙니다.",
            "나중에: 조건에 걸리면 [KICA] 제목으로 옵니다. 평소엔 조용합니다.",
            "익절/줄일 때: 해당 없음.",
            "하지 말 것: 이 알림을 매매 신호로 해석.",
        ]
    )
    send(cfg, "[KICA] 설치 확인 | 매매 신호 아님", body, 3, False)


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    dry = "dry" in args
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    state = load_json(STATE_PATH, DEFAULT_STATE)
    if "install" in args:
        install(cfg)
        return 0
    if not args or "daily" in args:
        daily(cfg, state, now, dry)
        return 0
    mode = "force" if "force" in args else ("dry" if dry else "auto")
    print(f"{now.isoformat()} mode={mode}")
    earnings(cfg, state, now, mode, dry)
    if dry:
        print("--- daily preview (dry) ---")
        daily(cfg, state, now, True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
