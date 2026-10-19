#!/usr/bin/env python3
"""한국정보인증(053300) watch.

  kica_monitor.py            daily (flow slot 15:40): silent unless a condition hits
  kica_monitor.py daily dry  same, prints only (no ntfy, no state write)
  kica_monitor.py auto       Q3 2026 or annual 2026 earnings one-shot (oneshot_pick passes "auto").
                             Q3 candidates 11-12/13/16 2026. Annual candidates 3-16/17/19 2027.
                             Sends once on the first day results are found; silent if not yet published,
                             except on the last candidate day (one "미게시" notice). Silent after a send.
                             Includes dividend-focused holding re-evaluation (yield>=3.5%, OP growth>=5%).
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
    "annual_notified": False,
    "annual_empty_notified": False,
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


def valuation(cfg: dict) -> tuple[str, float | None]:
    try:
        j = json.loads(fetch(cfg["sources"]["integration"]))
        m = {x.get("code"): x.get("value") for x in j.get("totalInfos", [])}
        dy_str = m.get("dividendYieldRatio") or ""
        dy = None
        if dy_str:
            try:
                dy = float(str(dy_str).replace("%", "").replace(",", "").strip())
            except ValueError:
                pass
        txt = f"PER {m.get('per')}, PBR {m.get('pbr')}, 배당수익률 {dy_str}"
        return txt, dy
    except Exception as exc:
        return f"밸류 조회 실패({exc.__class__.__name__})", None


def daily(cfg: dict, state: dict, now: datetime, dry: bool) -> None:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year not in (2026, 2027):
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
    val_txt, _ = valuation(cfg)
    body = "\n".join(
        [
            f"{stamp} | 053300",
            "이게 뭐 알림: 한국정보인증 종가가 정해둔 조건(하루 5% 이상 변동, 4,500~6,500원 밖)에 걸렸습니다. 매매 신호가 아닙니다.",
            f"지금 결과/상태: {day} 종가 {close:,}원 (전일 {prev['close']:,}원, {chg:+.2f}%). {val_txt}.",
            f"지금 할 일: {action}",
            "나중에: 3분기 분기보고서(11월 12~16일 예상)과 연간 실적(2027년 3월 중순 예상)에서 영업이익을 확인합니다. 밴드 알림은 종가가 안쪽으로 100원 돌아오면 다시 열립니다.",
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
        return None
    rows = {r["title"]: (r["columns"].get(key) or {}).get("value") for r in fi["rowList"]}
    op = num(rows.get("영업이익"))
    if op is None:
        return None
    return {"op": op, "rev": num(rows.get("매출액")), "ni": num(rows.get("당기순이익"))}


def annual(cfg: dict) -> dict | None:
    key = cfg["annual_earnings"]["key"]
    fi = json.loads(fetch(cfg["sources"]["annual"]))["financeInfo"]
    cons = {t["key"]: t.get("isConsensus") == "Y" for t in fi["trTitleList"]}
    if key not in cons or cons[key]:
        return None
    rows = {r["title"]: (r["columns"].get(key) or {}).get("value") for r in fi["rowList"]}
    op = num(rows.get("영업이익"))
    if op is None:
        return None
    return {"op": op, "rev": num(rows.get("매출액")), "ni": num(rows.get("당기순이익"))}


def last_oneshot_day(cfg: dict, kind: str = "q3") -> str:
    days = sorted(str(t)[:10] for t in (cfg.get("oneshot_kst") or []))
    if kind == "annual":
        return days[-1] if days else cfg["annual_earnings"]["expire_date"]
    return days[2] if len(days) > 2 else cfg["earnings"]["expire_date"]


def earnings(cfg: dict, state: dict, now: datetime, mode: str, dry: bool) -> None:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    e = cfg["earnings"]
    expire = datetime.fromisoformat(e["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    if mode == "auto" and (now.year != 2026 or now > expire):
        print("kica q3 earnings outside window; silent")
        return
    if state.get("earnings_notified") and mode != "force":
        print("kica q3 earnings already notified; silent")
        return
    try:
        q = quarter(cfg)
    except Exception as exc:
        fail_once(state, cfg, now, f"3분기 실적 조회 실패: {exc}", dry)
        save(state, dry)
        return
    if q is None:
        final_day = last_oneshot_day(cfg, "q3")
        if mode == "auto" and now.strftime("%Y-%m-%d") < final_day:
            print(f"kica q3 earnings not published yet; silent until final candidate {final_day}")
            return
        if state.get("empty_notified") and not dry:
            print("kica q3 earnings not published; already told")
            return
        body = "\n".join(
            [
                f"{stamp} | 053300",
                "이게 뭐 알림: 한국정보인증 3분기 실적을 확인하려 했는데 아직 게시되지 않았습니다.",
                "지금 결과/상태: 네이버 분기 실적에 2026.09 확정치가 없습니다. 공시가 아직 없거나 반영이 늦은 것입니다.",
                "지금 할 일: DART에서 한국정보인증 분기보고서가 올라왔는지 직접 확인하세요.",
                "나중에: 11월 12일, 13일, 16일 후보일에도 확인되지 않았습니다. 이후 자동 재확인은 없습니다. 지연 공시나 정정 여부를 확인하세요.",
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
    val_txt, dy = valuation(cfg)
    dy_ok = dy is not None and dy >= 3.5
    op_ok = yoy >= 5.0
    logic = "유지" if (dy_ok and op_ok) else "재검토"
    if yoy >= 10:
        tone = "작년 3분기보다 늘었습니다"
    elif yoy <= -10:
        tone = "작년 3분기보다 줄었습니다"
    else:
        tone = "작년 3분기와 비슷합니다"
    title = f"[KICA] 3분기 OP {op:,.0f}억 | YoY {yoy:+.0f}% | 배당중심 {logic}"
    rev_txt = f"매출 {rev:,.0f}억, " if rev is not None else ""
    ni_txt = f", 순이익 {ni:,.0f}억" if ni is not None else ""
    dy_note = f"배당수익률 {dy:.1f}% (기준 3.5% {'충족' if dy_ok else '미달'})" if dy is not None else "배당수익률 조회 실패"
    op_note = f"영업이익 YoY {yoy:+.1f}% (기준 5% {'충족' if op_ok else '미달'})"
    body = "\n".join(
        [
            f"{stamp} | 053300",
            "이게 뭐 알림: 한국정보인증 2026년 3분기 실적(연결) 결과와 배당 중심 보유 논리 재평가입니다. 매매 신호가 아닙니다.",
            f"지금 결과/상태: {rev_txt}영업이익 {op:,.0f}억{ni_txt}. 전년 3분기 {prev_q3:,.0f}억 대비 {yoy:+.0f}%, 2분기 {q2:,.0f}억 대비 {qoq:+.0f}%. {tone}. 1~3분기 누적 영업이익은 약 {h1 + op:,.0f}억(상반기 {h1:,.0f}억 기준). {val_txt}.",
            f"지금 할 일: 배당 중심 논리 {logic}. {dy_note}. {op_note}. 충족이면 기존 논리 유지, 미달이면 배당 지속과 본업 성장을 재점검하세요.",
            "나중에: 연간 실적과 배당 정책은 2027년 3월 중순에 확인합니다. 연간 기준은 그때 다시 확인합니다.",
            "익절/줄일 때: 이 알림은 주문이 아닙니다. 실적이 좋아도 줄일 기준은 본인이 미리 정한 값으로 판단하세요.",
            "하지 말 것: 한 분기 숫자만 보고 추격매수. 신용/미수.",
        ]
    )
    send(cfg, title, body, 4, dry)
    if not dry:
        state["earnings_notified"] = True
        state["last_sent"] = {"at": stamp, "hits": ["earnings"], "op": op, "yoy": round(yoy, 1), "dy": dy}
    save(state, dry)


def annual_earnings(cfg: dict, state: dict, now: datetime, mode: str, dry: bool) -> None:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    ae = cfg["annual_earnings"]
    expire = datetime.fromisoformat(ae["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    if mode == "auto" and (now.year != 2027 or now > expire):
        print("kica annual earnings outside window; silent")
        return
    if state.get("annual_notified") and mode != "force":
        print("kica annual earnings already notified; silent")
        return
    try:
        a = annual(cfg)
    except Exception as exc:
        fail_once(state, cfg, now, f"연간 실적 조회 실패: {exc}", dry)
        save(state, dry)
        return
    if a is None:
        final_day = last_oneshot_day(cfg, "annual")
        if mode == "auto" and now.strftime("%Y-%m-%d") < final_day:
            print(f"kica annual earnings not published yet; silent until final candidate {final_day}")
            return
        if state.get("annual_empty_notified") and not dry:
            print("kica annual earnings not published; already told")
            return
        body = "\n".join(
            [
                f"{stamp} | 053300",
                "이게 뭐 알림: 한국정보인증 2026년 연간 실적을 확인하려 했는데 아직 게시되지 않았습니다.",
                "지금 결과/상태: 네이버 연간 실적에 2026.12 확정치가 없습니다. 공시가 아직 없거나 반영이 늦은 것입니다.",
                "지금 할 일: DART에서 한국정보인증 사업보고서가 올라왔는지 직접 확인하세요.",
                "나중에: 2027년 3월 16일, 17일, 19일 후보일에도 확인되지 않았습니다. 이후 자동 재확인은 없습니다. 지연 공시나 정정 여부를 확인하세요.",
                "익절/줄일 때: 실적 숫자를 확인하기 전에는 판단하지 마세요.",
                "하지 말 것: 미게시를 호재나 악재로 해석.",
            ]
        )
        send(cfg, "[KICA] 연간 실적 미게시 | 직접 확인", body, 3, dry)
        if not dry:
            state["annual_empty_notified"] = True
        save(state, dry)
        return
    op, rev, ni = a["op"], a["rev"], a["ni"]
    prev = float(ae["prev_annual_op_eok"])
    yoy = (op - prev) / prev * 100.0
    val_txt, dy = valuation(cfg)
    dy_ok = dy is not None and dy >= 3.5
    op_ok = yoy >= 5.0
    logic = "유지" if (dy_ok and op_ok) else "재검토"
    if yoy >= 10:
        tone = "작년 연간보다 늘었습니다"
    elif yoy <= -10:
        tone = "작년 연간보다 줄었습니다"
    else:
        tone = "작년 연간과 비슷합니다"
    title = f"[KICA] 연간 OP {op:,.0f}억 | YoY {yoy:+.0f}% | 배당중심 {logic}"
    rev_txt = f"매출 {rev:,.0f}억, " if rev is not None else ""
    ni_txt = f", 순이익 {ni:,.0f}억" if ni is not None else ""
    dy_note = f"배당수익률 {dy:.1f}% (기준 3.5% {'충족' if dy_ok else '미달'})" if dy is not None else "배당수익률 조회 실패"
    op_note = f"영업이익 YoY {yoy:+.1f}% (기준 5% {'충족' if op_ok else '미달'})"
    body = "\n".join(
        [
            f"{stamp} | 053300",
            "이게 뭐 알림: 한국정보인증 2026년 연간 실적(연결) 결과와 배당 중심 보유 논리 재평가입니다. 매매 신호가 아닙니다.",
            f"지금 결과/상태: {rev_txt}영업이익 {op:,.0f}억{ni_txt}. 전년 연간 {prev:,.0f}억 대비 {yoy:+.0f}%. {tone}. {val_txt}.",
            f"지금 할 일: 배당 중심 논리 {logic}. {dy_note}. {op_note}. 충족이면 기존 논리 유지, 미달이면 배당 지속과 본업 성장을 재점검하세요.",
            "나중에: 배당 정책 확정액과 2027년 실적 추이를 지속 관찰합니다.",
            "익절/줄일 때: 이 알림은 주문이 아닙니다. 실적이 좋아도 줄일 기준은 본인이 미리 정한 값으로 판단하세요.",
            "하지 말 것: 한 해 숫자만 보고 추격매수. 신용/미수.",
        ]
    )
    send(cfg, title, body, 4, dry)
    if not dry:
        state["annual_notified"] = True
        state["last_sent"] = {"at": stamp, "hits": ["annual"], "op": op, "yoy": round(yoy, 1), "dy": dy}
    save(state, dry)


def install(cfg: dict) -> None:
    body = "\n".join(
        [
            "이게 뭐 알림: 한국정보인증(053300) 알림이 설치됐다는 확인입니다.",
            "지금 결과/상태: 평일 15:40 종가 점검(하루 5% 이상 변동 또는 4,500~6,500원 이탈 때만 발송)과 11월 13일 18:00 3분기 실적 확인, 2027년 3월 연간 실적 확인이 켜졌습니다. 실적 알림에 배당수익률 3.5% 이상, 영업이익 성장 5% 이상 기준 재평가가 포함됩니다.",
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
    if now.year == 2027 and now.month == 3:
        annual_earnings(cfg, state, now, mode, dry)
    else:
        earnings(cfg, state, now, mode, dry)
    if dry:
        print("--- daily preview (dry) ---")
        daily(cfg, state, now, True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
