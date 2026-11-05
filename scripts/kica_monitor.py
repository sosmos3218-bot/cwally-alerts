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

  kica_monitor.py selftest   offline checks (no network, no ntfy, no state write)

Daily conditions: KRX close moves +-move_pct vs the previous KRX close, or crosses a configured line
(configs/kica.json lines, 2026-11-05 pt76u): <=4,800 p3 1차 관심 점검 (re-arm >=5,000), <4,500 p3 2차 관심 점검
(>=4,700), >6,500 p3 공정가치 근처 추격 보류 (<6,200), <=3,950 p4 경고 (>=4,150, warning only, no buy wording).
Everything that fires on one day goes out as ONE message. KIND disclosure watch (자기주식 취득 결정, 주식 소각 결정,
현금 현물배당 결정, 대량보유): one p3 per new filing number, existing filings seeded as seen. Not a trade order.
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
    "disc_seen": [],
    "disc_seeded": None,
    "kind_fail": 0,
}
BUY_WORDS = ("매수 검토", "분할 매수", "매수 기회", "저가 매수", "사세요", "담을", "물타기", "비중 확대", "지금 매수", "관심 점검", "추가매수")
OPS = {"le": lambda v, x: v <= x, "lt": lambda v, x: v < x, "ge": lambda v, x: v >= x, "gt": lambda v, x: v > x}
NM = "[KICA] 한국정보인증 053300"


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
    send(cfg, NM + " 시세 조회 실패 | 판정 보류", body, 4, dry)
    if not dry:
        state["last_fail_day"] = day


def krx_bars(cfg: dict, now: datetime) -> list[dict]:
    import krx_close
    code = str(cfg.get("ticker") or cfg.get("code") or "053300")
    bars = list(reversed(krx_close.completed(krx_close.regular_bars(code, 15), now, (15, 35))))
    return [{"day": b.day, "close": int(round(b.close)), "volume": int(b.volume or 0)} for b in bars]


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


def judge_lines(cfg: dict, fired: dict, close: float) -> list[dict]:
    """fired[k] True = already sent, waiting for re-arm. Unknown keys seed silently from this close."""
    out = []
    for ln in cfg.get("lines") or []:
        k = ln["key"]
        hit = OPS[ln["op"]](close, ln["level"])
        if k not in fired:
            fired[k] = hit
        elif fired[k]:
            if OPS[ln["rearm_op"]](close, ln["rearm"]):
                fired[k] = False
        elif hit:
            fired[k] = True
            out.append(ln)
    return out


def footer(cfg: dict) -> str:
    return f"참고: {cfg.get('context', '')} 판단 변경 조건: {cfg.get('change_conditions', '')} 보유: {cfg.get('holding_note', '보유')}"


TODO = {
    "l4800": "120일선(11/5 기준 4,922원) 아래 종가. 점검: 하락 원인(공시, 3분기 실적, 업종 수급), 배당 200원 유지 가능성. 금액과 분할 계획부터 정하기.",
    "band_low": "4,500원 아래(배당 200원 기준 수익률 약 4.4%). 점검: 1차 점검 뒤 무엇이 바뀌었는지, 실적과 배당 정책 변화 여부.",
    "band_high": "Simply Wall St 공정가치(약 6,536원) 근처. 추격 보류. 밸류 매력 소멸 점검: 배당수익률 3%대 초반으로 내려왔는지, 이익 성장이 따라오는지 확인.",
    "w3950": "120일 저점(3,970원) 아래 종가. 논리 재점검: 인증서 매출 정체, 광고 자회사 부진, 순이익 감소, 배당 축소 중 무엇이 원인인지 먼저 확인. 가격 점검 선은 멈춤.",
}
DONT = {
    "l4800": "하루 하락만 보고 판단, 계획 없이 한 번에 늘리기.",
    "band_low": "배당수익률만 보고 판단, 신용 및 미수.",
    "band_high": "급등 추격, 하루 움직임만 보고 판단.",
    "w3950": "저점 이탈을 싸졌다는 신호로 읽기, 신용 및 미수.",
}


def compose(cfg: dict, stamp: str, day: str, close: int, prev: int, chg: float, move: str | None,
            lines: list[dict], val_txt: str) -> tuple[str, str, int]:
    warn = any(x.get("warn") for x in lines)
    shown = [x for x in lines if x.get("warn")] if warn else lines
    parts = ([f"하루 {chg:+.1f}% {'상승' if move == 'move_up' else '하락'}"] if move else []) + [x["title"] for x in shown]
    title = f"{NM} {close:,}원 {chg:+.1f}% | " + " + ".join(parts)
    if shown:
        todo = " ".join(TODO.get(x["key"], "") for x in shown)
        dont = " ".join(DONT.get(x["key"], "") for x in shown)
    else:
        todo = ("크게 빠졌습니다. 서두르지 말고 이유(공시, 뉴스)를 먼저 확인하세요." if move == "move_down"
                else "크게 올랐습니다. 이유(공시, 뉴스)를 먼저 확인하세요.")
        dont = "하루 움직임만 보고 추격. 신용/미수."
    if move and shown:
        todo = f"하루 {chg:+.1f}% 움직임도 함께 걸렸습니다. 이유(공시, 뉴스)부터 확인. " + todo
    body = "\n".join([
        f"{stamp} | 053300 | 한국정보인증",
        f"이게 뭐 알림: 한국정보인증 KRX 확정 종가가 정해둔 조건에 걸렸습니다(같은 날 걸린 조건은 한 건으로 합침). {'경고. ' if warn else ''}매매 신호가 아닙니다.",
        f"지금 결과/상태: {day} 종가 {close:,}원 (전일 {prev:,}원, {chg:+.2f}%). 걸린 조건: {', '.join(parts)}. {val_txt}. {footer(cfg)}",
        f"지금 할 일: {todo}",
        "나중에: 같은 선은 재무장 뒤에만 다시 옵니다(4,800 선은 5,000원 이상, 4,500 선은 4,700원 이상, 6,500 선은 6,200원 미만, 3,950 선은 4,150원 이상). "
        "3분기 분기보고서(11월 12~16일 예상), 연간 실적(2027년 3월 중순 예상), 공시 감시는 계속.",
        "익절/줄일 때: 보유 중(수량, 평단 미입력이라 손익 표시 없음). " + ("줄일지 유지할지는 미리 정한 기준으로 판단." if warn else "이 알림은 주문이 아닙니다. 줄일 기준은 본인이 미리 정한 값으로 판단."),
        f"하지 말 것: {dont}",
    ])
    if warn:
        bad = [w for w in BUY_WORDS if w in title + body]
        if bad:
            raise ValueError(f"buy wording in warning: {bad}")
    pri = max([x["priority"] for x in lines] + ([4] if move else []))
    return title, body, pri


def price_daily(cfg: dict, state: dict, now: datetime, dry: bool) -> None:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
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
    mv = float(cfg["levels"]["move_pct"])
    chg = (close - prev["close"]) / prev["close"] * 100.0
    fired = dict(state.get("fired") or {})
    print(stamp, "bar", day, "close", close, f"chg {chg:+.2f}%", "prev", prev["close"])
    base_day = str((cfg.get("baseline") or {}).get("day") or "")
    last_day = (state.get("last_bar") or {}).get("day")
    state["last_bar"] = {"day": day, "close": close, "chg_pct": round(chg, 2)}
    state["updated_at"] = stamp
    if day <= base_day:
        print("kica baseline day or earlier; silent")
        state["fired"] = fired
        save(state, dry)
        return
    if last_day == day and not dry:
        print("kica bar already judged today; silent")
        return
    lines = judge_lines(cfg, fired, close)
    move = None
    if abs(chg) >= mv and state.get("last_move_day") != day:
        move = "move_up" if chg > 0 else "move_down"
    print("kica fired", fired)
    if not lines and not move:
        state["fired"] = fired
        save(state, dry)
        print("kica silent; no condition")
        return
    val_txt, _ = valuation(cfg)
    title, body, pri = compose(cfg, stamp, day, close, prev["close"], chg, move, lines, val_txt)
    send(cfg, title, body, pri, dry)
    if not dry:
        if move:
            state["last_move_day"] = day
        state["last_sent"] = {"at": stamp, "hits": ([move] if move else []) + [x["key"] for x in lines]}
    state["fired"] = fired
    save(state, dry)


def kind_rows(since: str, until: str) -> list[dict]:
    import usdiv_monitor as u  # lazy: KIND 상세검색 재사용
    return u.kind_search("053300", "", since, until, 3)


def disc_match(cfg: dict, title: str) -> bool:
    w = cfg["disclosure_watch"]
    return any(re.search(p, title) for p in w["patterns"]) and not any(re.search(x, title) for x in w["exclude"])


def show(title: str) -> str:
    t = re.sub(r"^한국정보인증\s+", "", title).replace("\u318d", ", ").replace("\u00b7", ", ")
    return re.sub(r"\s+", " ", t).strip()


def disc_message(cfg: dict, r: dict, stamp: str) -> tuple[str, str, int]:
    t = show(r["title"])
    kind = ("대량보유 보고" if "대량보유" in t else "배당 결정" if "배당" in t else "자기주식 취득 결정" if "자기주식" in t else "주식 소각 결정")
    hint = {"대량보유 보고": "보고자와 지분 변동(다우기술 외 특수관계인 지분 약 55%의 변화, 5% 이상 신규 주주인지) 확인.",
            "배당 결정": "주당 배당금(직전 200원)과 기준일 확인. 배당 축소면 판단 변경 조건.",
            "자기주식 취득 결정": "매입 규모와 기간, 신탁인지 직접 취득인지, 소각 계획 여부 확인.",
            "주식 소각 결정": "소각 주식 수와 발행주식 대비 비율 확인."}[kind]
    url = cfg["sources"]["kind_view"].format(acptno=r["acptno"])
    body = "\n".join([
        f"{stamp} | 053300 | 한국정보인증",
        f"이게 뭐 알림: 한국정보인증 새 공시({kind}) 1회 알림. 내용 판단은 직접. 매매 신호가 아닙니다.",
        f"지금 결과/상태: {t} ({r['filed']}, 접수번호 {r['acptno']}). 원문: {url} {footer(cfg)}",
        f"지금 할 일: {hint}",
        "나중에: 같은 접수번호는 다시 보내지 않습니다. 가격 감시와 실적 확인은 계속.",
        "익절/줄일 때: 보유 중(수량, 평단 미입력). 공시만으로 줄이거나 늘리지 말고 내용 확인 뒤 판단.",
        "하지 말 것: 제목만 보고 매매, 공시 직후 급등락 추격.",
    ])
    return f"{NM} 공시 | {kind}: {t[:60]}", body, int(cfg["disclosure_watch"]["priority"])


def disclosures(cfg: dict, state: dict, now: datetime, dry: bool) -> None:
    w = cfg.get("disclosure_watch")
    if not w:
        return
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = stamp[:10]
    seen = list(state.get("disc_seen") or [])
    try:
        since = (now.date() - timedelta(days=int(w["lookback_days"]))).isoformat()
        rows = kind_rows(since, today)
        hits = [r for r in rows if disc_match(cfg, r["title"])]
        seeding = not state.get("disc_seeded")
        for r in sorted(hits, key=lambda x: x["acptno"]):
            if r["acptno"] in seen:
                continue
            seen.append(r["acptno"])
            if not seeding:
                t, b, pri = disc_message(cfg, r, stamp)
                send(cfg, t, b, pri, dry)
        if seeding:
            state["disc_seeded"] = stamp
            print(f"kica disclosures seeded: {len(hits)} matches marked seen")
        state["disc_seen"] = seen[-200:]
        state["kind_fail"] = 0
        print(f"kica KIND rows {len(rows)}, matches {len(hits)}")
    except Exception as exc:
        state["kind_fail"] = int(state.get("kind_fail") or 0) + 1
        print(f"kica KIND fail {state['kind_fail']}: {exc}")
        if state["kind_fail"] >= 3 and state.get("kind_fail_day") != today:
            state["kind_fail_day"] = today
            send(cfg, f"{NM} 공시 조회 실패 | 공시 감시 보류", "\n".join([
                f"{stamp} | 053300 | 한국정보인증", "이게 뭐 알림: KIND 공시 조회 3회 연속 실패 안내. 매매 신호가 아닙니다.",
                f"지금 결과/상태: KIND 확인 불가 ({str(exc)[:150]}).", "지금 할 일: 필요하면 DART 에서 직접 확인.",
                "나중에: 복구되면 최근 45일 공시를 다시 보고 새 접수번호만 보냅니다.", "익절/줄일 때: 해당 없음.",
                "하지 말 것: 조회 실패를 공시 없음으로 읽기."]), 3, dry)
    save(state, dry)


def daily(cfg: dict, state: dict, now: datetime, dry: bool) -> None:
    if now.year not in (2026, 2027):
        print("kica year-guard silent")
        return
    if now.weekday() >= 5:
        print("kica weekend silent")
        return
    if now.strftime("%H:%M") < "15:35" and not dry:
        print("kica before KRX close; silent")
        return
    price_daily(cfg, state, now, dry)
    disclosures(cfg, state, now, dry)


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
        send(cfg, NM + " 3분기 실적 미게시 | 직접 확인", body, 3, dry)
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
    title = f"{NM} 3분기 OP {op:,.0f}억 | YoY {yoy:+.0f}% | 배당중심 {logic}"
    rev_txt = f"매출 {rev:,.0f}억, " if rev is not None else ""
    ni_txt = f", 순이익 {ni:,.0f}억" if ni is not None else ""
    dy_note = f"배당수익률 {dy:.1f}% (기준 3.5% {'충족' if dy_ok else '미달'})" if dy is not None else "배당수익률 조회 실패"
    op_note = f"영업이익 YoY {yoy:+.1f}% (기준 5% {'충족' if op_ok else '미달'})"
    body = "\n".join(
        [
            f"{stamp} | 053300",
            "이게 뭐 알림: 한국정보인증 2026년 3분기 실적(연결) 결과와 배당 중심 보유 논리 재평가입니다. 매매 신호가 아닙니다.",
            f"지금 결과/상태: {rev_txt}영업이익 {op:,.0f}억{ni_txt}. 전년 3분기 {prev_q3:,.0f}억 대비 {yoy:+.0f}%, 2분기 {q2:,.0f}억 대비 {qoq:+.0f}%. {tone}. 1~3분기 누적 영업이익은 약 {h1 + op:,.0f}억(상반기 {h1:,.0f}억 기준). {val_txt}.",
            f"지금 할 일: 배당 중심 논리 {logic}. {dy_note}. {op_note}. 3분기 점검 기준: {e.get('q3_criteria', '')}. 충족이면 기존 논리 유지, 미달이면 배당 지속과 본업 성장을 재점검하세요. 판단 변경 조건: {cfg.get('change_conditions', '')}",
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
        send(cfg, NM + " 연간 실적 미게시 | 직접 확인", body, 3, dry)
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
    title = f"{NM} 연간 OP {op:,.0f}억 | YoY {yoy:+.0f}% | 배당중심 {logic}"
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
    send(cfg, NM + " 설치 확인 | 매매 신호 아님", body, 3, False)


def selftest(cfg: dict) -> int:
    g = globals()
    fails = 0
    sent: list = []

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    bars: list = []
    kind: list = []
    orig = {k: g[k] for k in ("krx_bars", "valuation", "kind_rows", "send", "save")}
    g["krx_bars"] = lambda c, n: [{"day": d, "close": v, "volume": 0} for d, v in reversed(bars)]
    g["valuation"] = lambda c: ("PER 8.0, PBR 0.98, 배당수익률 4.06%", 4.06)
    g["kind_rows"] = lambda a, b: list(kind)
    g["send"] = lambda c, t, b, p, d: sent.append((t, b, p))
    g["save"] = lambda st, d: None
    try:
        f: dict = {}
        check("seed at 4,930 silent, none fired", (judge_lines(cfg, f, 4930), f), ([], {"l4800": False, "band_low": False, "band_high": False, "w3950": False}))
        walk = [4800, 4700, 4999, 5000, 4490, 4690, 4700, 6510, 6300, 6199, 6600, 3950, 4140, 4150, 3900]
        f = {"l4800": False, "band_low": False, "band_high": False, "w3950": False}
        got = [[x["key"] for x in judge_lines(cfg, f, v)] for v in walk]
        check("walk with re-arm buffers", got, [["l4800"], [], [], [], ["l4800", "band_low"], [], [], ["band_high"], [], [], ["band_high"],
                                                ["l4800", "band_low", "w3950"], [], [], ["w3950"]])
        L = {x["key"]: x for x in cfg["lines"]}
        t, b, p = compose(cfg, "s", "2026-11-10", 4490, 4800, -6.46, "move_down", [L["l4800"], L["band_low"]], "v")
        check("merged one message, title", (t, p), ("[KICA] 한국정보인증 053300 4,490원 -6.5% | 하루 -6.5% 하락 + 1차 관심 점검 (120일선 이탈) + 2차 관심 점검 (4,500원 하단, 배당수익률 약 4.4%)", 4))
        tw, bw, pw = compose(cfg, "s", "2026-11-10", 3900, 4200, -7.1, "move_down", [L["l4800"], L["band_low"], L["w3950"]], "v")
        check("warning only, p4, no buy wording", (tw.endswith("| 하루 -7.1% 하락 + 경고 120일 저점 이탈, 논리 재점검"), pw, [w for w in BUY_WORDS if w in tw + bw]), (True, 4, []))
        check("body has change conditions and holding note", all(k in b for k in ("상반기 +1.6%", "광고 자회사", "순이익 감소 지속", "배당 축소", "2030년 이후", "평단 미입력", "6,536원")), True)
        th, _, ph = compose(cfg, "s", "d", 6510, 6400, 1.7, None, [L["band_high"]], "v")
        check("upper band p3 wording", (th, ph), ("[KICA] 한국정보인증 053300 6,510원 +1.7% | 6,500원 상단: 공정가치 근처, 추격 보류, 밸류 매력 소멸 점검", 3))
        check("six-part body", [x.split(":")[0] for x in b.split("\n")[1:]], ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
        st = {"fired": {"band_high": False, "band_low": False}, "last_bar": {"day": "2026-11-04"}}
        bars[:] = [("2026-11-04", 5020), ("2026-11-05", 4930)]
        kind[:] = [{"acptno": "20260820000316", "filed": "2026-08-20 15:15", "title": "한국정보인증 자기주식취득 신탁계약 체결 결정 한국정보인증"},
                   {"acptno": "20260902000482", "filed": "2026-09-02 16:56", "title": "한국정보인증 변경상장(주식소각) 코스닥시장본부"},
                   {"acptno": "20260710000154", "filed": "2026-07-10 09:20", "title": "한국정보인증 신탁계약에의한 취득 상황보고서 한국정보인증"}]
        sent.clear()
        daily(cfg, st, datetime(2026, 11, 5, 15, 40, tzinfo=KST), False)
        check("first run: new lines seeded, disclosures seeded, silent", (sent, st["disc_seen"], st["fired"]["l4800"]), ([], ["20260820000316"], False))
        bars.append(("2026-11-06", 4460))
        kind += [{"acptno": "20261113000500", "filed": "2026-11-13 16:00", "title": "한국정보인증 주식소각 결정 한국정보인증"},
                 {"acptno": "20261113000501", "filed": "2026-11-13 16:01", "title": "한국정보인증 현금\u318d현물배당 결정 한국정보인증"},
                 {"acptno": "20261113000502", "filed": "2026-11-13 16:02", "title": "한국정보인증 자기주식취득 신탁계약 해지 결정 한국정보인증"}]
        daily(cfg, st, datetime(2026, 11, 6, 15, 40, tzinfo=KST), False)
        ts = [x[0] for x in sent]
        check("one merged price alert + 2 disclosures (해지 excluded)", (len(sent), ts[0].count(" | "), sum(" 공시 |" in t for t in ts)), (3, 1, 2))
        allt = "".join(x[0] + x[1] for x in sent)
        check("no middle dot or em-dash, ㆍ rewritten", ("\u00b7" in allt, "\u2014" in allt, "\u318d" in allt, "현금, 현물배당 결정" in allt), (False, False, False, True))
        check("all titles [KICA] 한국정보인증 053300", all(t.startswith(NM) for t in ts), True)
        sent.clear()
        daily(cfg, st, datetime(2026, 11, 6, 15, 50, tzinfo=KST), False)
        check("re-run same day silent", sent, [])
    finally:
        g.update(orig)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    dry = "dry" in args
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    state = load_json(STATE_PATH, DEFAULT_STATE)
    if "selftest" in args:
        return selftest(cfg)
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
