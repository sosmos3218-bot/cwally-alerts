#!/usr/bin/env python3
"""두산(000150) 3Q26 실적 후 SOTP 민감도 알림.

  dssotp_monitor.py          auto (flow 슬롯 평일 15:40): 3분기 연결 잠정실적 공정공시가 처음 확인된 날
                             한 번만 보낸다. 없으면 침묵. expire_date(11-16)까지 없으면 미게시 안내 1회(cwally-ops).
  dssotp_monitor.py dry      공시 탐지, 대만 3사 배수, 민감도 표를 출력만 (ntfy 없음, state 쓰기 없음).
                             2분기 공시로 파서 자체 점검도 출력.
  dssotp_monitor.py force    공시가 없어도 표를 계산해 보낸다 (수동 실행은 채팅 승인 필요).
  dssotp_monitor.py install  설치 확인 1회 발송 (AGENTS.md), state 쓰기 없음.

대만 CCL 3사(EMC 2383, TUC 6274, ITEQ 6213) 2027E EV/EBITDA는 기준일(9-30) 컨센서스 배수에
주가 변화율을 곱한 근사다. 매매 신호가 아니다.
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402
from naver_api import notices  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "dssotp.json"
STATE_PATH = ROOT / "state" / "dssotp.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
DEFAULT_STATE = {
    "updated_at": None,
    "notified": False,
    "empty_notified": False,
    "last_fail_day": None,
    "last_sent": None,
}
UNIT_TO_EOK = {"백만원": 100.0, "억원": 1.0, "천원": 100000.0, "조원": 0.0001}


# ---------- io ----------

def load_json(path: Path, fallback: dict) -> dict:
    out = json.loads(json.dumps(fallback))
    if not path.exists():
        return out
    try:
        out.update(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        pass
    return out


def save(state: dict, now: datetime, dry: bool) -> None:
    if dry:
        return
    state["updated_at"] = now.strftime("%Y-%m-%d %H:%M KST")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# TWSE/TPEx 인증서에 Subject Key Identifier가 없어 Python 3.13+ 기본 X509_STRICT 검증이 실패한다.
# 체인과 호스트 검증은 그대로 두고 STRICT 플래그만 끈다.
_SSL = ssl.create_default_context()
_SSL.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def send(cfg: dict, title: str, body: str, pri: int, dry: bool) -> None:
    print(f"TITLE: {title}\n{body}")
    print(f"(body bytes: {len(body.encode('utf-8'))})")
    if dry:
        print("DRY RUN: no ntfy sent")
        return
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title[:180],
        body,
        pri,
        ["bar_chart"],
        resolve_server(cfg.get("ntfy_server")),
    )


# ---------- 공시 ----------

def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_notice(body: str, period_end: str) -> dict | None:
    """연결 매출/영업이익(억원)과 (주)두산 별도 매출/영업이익(억원)."""
    m = re.search(r"당기실적\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", body or "")
    if not m or m.group(2) != period_end:
        return None
    um = re.search(r"단위\s*[:：]\s*(조원|억원|백만원|천원)", body)
    if not um:
        return None
    div = UNIT_TO_EOK[um.group(1)]
    num_re = r"(-?[\d,]+(?:\.\d+)?)"
    op = re.search(r"영업이익\s*당해실적\s*" + num_re, body)
    rev = re.search(r"매출액\s*당해실적\s*" + num_re, body)
    if not op:
        return None
    out = {
        "op": round(_num(op.group(1)) / div),
        "rev": round(_num(rev.group(1)) / div) if rev else None,
        "sep_op": None,
        "sep_rev": None,
    }
    sop = re.search(r"■\s*영업이익\s*[:：]\s*(-?[\d,]+)\s*억원", body)
    srev = re.search(r"■\s*매출액\s*[:：]\s*(-?[\d,]+)\s*억원", body)
    if sop:
        out["sep_op"] = round(_num(sop.group(1)))
    if srev:
        out["sep_rev"] = round(_num(srev.group(1)))
    return out


def find_result(cfg: dict, period_end: str, since: str) -> dict | None:
    key = cfg["trigger"]["title_key"]
    for r in notices(cfg["ticker"], 60):
        title = r.get("title", "")
        if key not in title or "정정" in title:
            continue
        if str(r.get("datetime", ""))[:10] < since:
            continue
        got = parse_notice(r.get("text", ""), period_end)
        if got:
            got.update(title=title, datetime=r.get("datetime"))
            return got
    return None


# ---------- 시세 ----------

def krx_close(cfg: dict, code: str, now: datetime) -> tuple[str, float]:
    url = cfg["sources"]["krx_daily"].format(
        code=code, start=(now - timedelta(days=20)).strftime("%Y%m%d"), end=now.strftime("%Y%m%d")
    )
    rows = re.findall(r'\["(\d{8})",\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)', fetch(url))
    bars = sorted(((d, float(c)) for d, o, h, lo, c in rows), reverse=True)
    today = now.strftime("%Y%m%d")
    if bars and bars[0][0] == today and now.strftime("%H:%M") < "15:35":
        bars = bars[1:]  # 장중 막대는 종가가 아니다
    if not bars:
        raise RuntimeError(f"{code} 시세 없음")
    d, c = bars[0]
    return f"{d[:4]}-{d[4:6]}-{d[6:]}", c


def tw_live(items: list[dict]) -> dict:
    chans = "|".join(f"{it['market']}_{it['code']}.tw" for it in items)
    raw = json.loads(fetch(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={chans}"))
    out = {}
    for m in raw.get("msgArray") or []:
        px = None
        for k in ("z", "pz"):
            try:
                px = float(m.get(k))
                break
            except (TypeError, ValueError):
                continue
        d = str(m.get("d") or "")
        if px and len(d) == 8:
            out[m.get("c")] = (f"{d[:4]}-{d[4:6]}-{d[6:]}", px)
    return out


def tw_daily(it: dict, now: datetime) -> tuple[str, float]:
    ym = now.strftime("%Y%m")
    if it["market"] == "otc":
        raw = json.loads(fetch(
            f"https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={it['code']}&date={ym[:4]}/{ym[4:]}/01&response=json"
        ))
        rows = (raw.get("tables") or [{}])[0].get("data") or []
    else:
        raw = json.loads(fetch(
            f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={ym}01&stockNo={it['code']}"
        ))
        rows = raw.get("data") or []
    if not rows:
        raise RuntimeError(f"{it['name']} 일별 시세 없음")
    last = rows[-1]
    y, mo, dd = last[0].split("/")
    return f"{int(y) + 1911}-{mo}-{dd}", float(str(last[6]).replace(",", ""))


def peer_multiples(cfg: dict, now: datetime) -> tuple[list[dict], list[str]]:
    items = cfg["peers"]["items"]
    errs: list[str] = []
    live: dict = {}
    try:
        live = tw_live(items)
    except Exception as exc:
        errs.append(f"TWSE 실시간: {exc.__class__.__name__}")
    out = []
    for it in items:
        got = live.get(it["code"])
        if not got:
            try:
                got = tw_daily(it, now)
            except Exception as exc:
                errs.append(f"{it['name']}: {exc.__class__.__name__}")
                continue
        day, px = got
        mult = float(it["ev_ebitda_2027e"]) * px / float(it["base_price"])
        out.append({"name": it["name"], "day": day, "price": px, "chg": px / float(it["base_price"]) - 1, "mult": mult})
    return out, errs


# ---------- SOTP ----------

def sotp_inputs(cfg: dict, now: datetime) -> dict:
    s = cfg["sotp"]
    day, px = krx_close(cfg, cfg["ticker"], now)
    stakes = float(s["other_stakes_eok"]) * 1e8
    listed = []
    for it in s["listed"]:
        d, c = krx_close(cfg, it["code"], now)
        v = c * float(it["shares_held"])
        stakes += v
        listed.append((it["name"], v))
    pref = 0.0
    for it in s["preferred"]:
        _, c = krx_close(cfg, it["code"], now)
        pref += c * float(it["shares"])
    return {
        "day": day,
        "price": px,
        "stakes": stakes,
        "listed": listed,
        "pref": pref,
        "ebitda": float(s["ebg_ebitda_2027e_eok"]) * 1e8,
        "siltron": float(s["siltron_eok"]) * 1e8,
        "net_debt": float(s["net_debt_eok"]) * 1e8,
        "shares": float(s["common_shares_out"]),
    }


def per_share(x: dict, mult: float, disc_pct: float) -> float:
    eq = x["ebitda"] * mult + x["siltron"] + x["stakes"] * (1 - disc_pct / 100) - x["net_debt"] - x["pref"]
    return eq / x["shares"]


def implied_mult(x: dict, disc_pct: float) -> float:
    mcap = x["price"] * x["shares"]
    ev_ebg = mcap + x["pref"] + x["net_debt"] - x["siltron"] - x["stakes"] * (1 - disc_pct / 100)
    return ev_ebg / x["ebitda"]


def man(won: float) -> str:
    return f"{won / 1e4:,.0f}"


def grid_lines(cfg: dict, x: dict, avg: float | None) -> list[str]:
    s = cfg["sotp"]
    ds = s["discounts_pct"]
    near = min(s["multiples"], key=lambda m: abs(m - avg)) if avg else None
    lines = ["표(주당 만원) 배수\\할인 " + " ".join(f"{d}%" for d in ds)]
    for m in s["multiples"]:
        mark = "*" if m == near else " "
        lines.append(f"{mark}{m}배 " + " ".join(f"{man(per_share(x, m, d)):>4}" for d in ds))
    return lines


def valuation_block(cfg: dict, now: datetime) -> tuple[list[str], dict]:
    peers, errs = peer_multiples(cfg, now)
    x = sotp_inputs(cfg, now)
    s, p = cfg["sotp"], cfg["peers"]
    avg = sum(q["mult"] for q in peers) / len(peers) if peers else None
    base_d, cons_d = float(s["base_discount_pct"]), float(s["conservative_discount_pct"])
    px = x["price"]
    lines = []
    if peers:
        lines.append(
            "대만 3사 2027E EV/EBITDA(근사): "
            + ", ".join(f"{q['name']} {q['mult']:.1f}배({q['chg']:+.0%})" for q in peers)
            + f", 평균 {avg:.1f}배. 괄호는 9-30 대비 주가 변화."
        )
    if errs:
        lines.append("대만 시세 일부 조회 실패: " + "; ".join(errs) + ". 남은 회사로만 평균.")
    base_day = datetime.fromisoformat(p["baseline_date"]).replace(tzinfo=KST)
    age = (now - base_day).days
    if age > int(p.get("stale_days") or 45):
        lines.append(f"주의: 피어 추정치는 {p['baseline_date']} 기준({age}일 지남). 3분기 실적 후 바뀐 컨센서스는 미반영.")
    lines.extend(grid_lines(cfg, x, avg))
    summary = {"price": px, "avg": avg}
    if avg:
        b = per_share(x, avg, base_d)
        c = per_share(x, avg, cons_d)
        summary.update(base=b, cons=c)
        lines.append(
            f"기준(피어 평균 {avg:.1f}배, 할인 {base_d:.0f}%): 약 {man(b)}만원({b / px - 1:+.0%}). "
            f"보수(할인 {cons_d:.0f}%): 약 {man(c)}만원({c / px - 1:+.0%})."
        )
    lines.append(
        f"현재가 {px / 1e4:,.1f}만원({x['day']})에 담긴 전자BG 배수: 할인 {base_d:.0f}%면 {implied_mult(x, base_d):.1f}배, "
        f"{cons_d:.0f}%면 {implied_mult(x, cons_d):.1f}배."
    )
    lines.append(
        f"가정: 전자BG 2027E EBITDA {s['ebg_ebitda_2027e_eok'] / 1e4:.2f}조, 실트론 {s['siltron_eok'] / 1e4:.1f}조(인수가), "
        f"순차입 {s['net_debt_eok'] / 1e4:.1f}조, 지분가치 {x['stakes'] / 1e12:.1f}조(할인 전), 우선주 {x['pref'] / 1e12:.1f}조 차감. *는 피어 평균에 가장 가까운 줄."
    )
    return lines, summary


# ---------- 알림 ----------

def fail_once(state: dict, cfg: dict, now: datetime, note: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    if state.get("last_fail_day") == day and not dry:
        print("fail already notified today")
        return
    body = "\n".join(
        [
            f"{now.strftime('%Y-%m-%d %H:%M KST')} | 000150",
            "이게 뭐 알림: 두산 3분기 실적 후 SOTP 계산 중 조회가 실패했습니다.",
            f"지금 결과/상태: {note}",
            "지금 할 일: 실패를 실적이나 주가 신호로 보지 마세요. 공시는 DART에서 직접 확인하세요.",
            "나중에: 다음 평일 15:40 KST에 다시 시도합니다.",
            "익절/줄일 때: 이 실패로 사거나 팔지 마세요.",
            "하지 말 것: 조회 실패를 호재나 악재로 해석.",
        ]
    )
    send(cfg, "[DSSOTP] 000150 시세 조회 실패 | 판정 보류", body, 3, dry)
    if not dry:
        state["last_fail_day"] = day


def result_alert(cfg: dict, state: dict, now: datetime, res: dict | None, dry: bool) -> None:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    t = cfg["trigger"]
    pv = t["prev"]
    try:
        vlines, sm = valuation_block(cfg, now)
    except Exception as exc:
        fail_once(state, cfg, now, f"시세/SOTP 계산 실패: {exc}", dry)
        save(state, now, dry)
        return
    if res:
        rv = f"매출 {res['rev']:,}억, " if res.get("rev") is not None else ""
        sep = ""
        if res.get("sep_op") is not None:
            sep = f" (주)두산 별도 영업이익 {res['sep_op']:,}억({pv['label']} {pv['sep_op_eok']:,}억)."
        head = f"3분기 연결 {rv}영업이익 {res['op']:,}억({pv['label']} {pv['op_eok']:,}억).{sep}"
    else:
        head = "3분기 잠정실적 공시 없이 계산한 표입니다(미리보기/수동)."
    if sm.get("base"):
        title = f"[DSSOTP] 000150 3분기 후 SOTP 갱신 | 기준 {man(sm['base'])}만원, 표 확인"
    else:
        title = "[DSSOTP] 000150 3분기 후 SOTP 갱신 | 피어 배수 없음, 표 확인"
    body = "\n".join(
        [
            f"{stamp} | 000150",
            "이게 뭐 알림: 두산 3분기 잠정실적이 나와 전자BG 배수 x 지분 할인율 민감도를 다시 계산했습니다. 매매 신호가 아닙니다.",
            f"지금 결과/상태: {head}",
            *vlines,
            f"지금 할 일: IR 자료에서 전자BG 3분기 영업이익(메리츠 추정 {t['ebg_q3_op_est_eok']:,}억)과 4분기 가이던스를 확인하세요. 피어 평균이 14배 아래면 보수 줄로 보세요.",
            "나중에: 대만 3사 3분기 실적 후 2027E 추정치가 바뀌면 기준값 갱신이 필요합니다. 실트론 인수 종결은 2027-01-31 예정입니다.",
            "익절/줄일 때: 이 표는 주문이 아닙니다. 매매는 본인이 미리 정한 기준으로 판단하세요.",
            "하지 말 것: 표의 가장 큰 숫자를 목표가로 쓰기. 신용/미수.",
        ]
    )
    send(cfg, title, body, 3, dry)
    if not dry:
        state["notified"] = True
        state["last_sent"] = {"at": stamp, "op": (res or {}).get("op"), "base": sm.get("base"), "avg": sm.get("avg")}
    save(state, now, dry)


def empty_alert(cfg: dict, state: dict, now: datetime, dry: bool) -> None:
    if state.get("empty_notified") and not dry:
        print("dssotp empty already told")
        return
    body = "\n".join(
        [
            f"{now.strftime('%Y-%m-%d %H:%M KST')} | 000150",
            "이게 뭐 알림: 두산 3분기 잠정실적 공시를 찾지 못했습니다.",
            "지금 결과/상태: 네이버 공시 목록에 2026-07-01~09-30 연결 잠정실적 공정공시가 없습니다.",
            "지금 할 일: DART에서 두산 3분기 보고서나 잠정실적 공시를 직접 확인하세요.",
            "나중에: 이후 자동 재확인은 없습니다. 민감도 표가 필요하면 수동 실행을 요청하세요.",
            "익절/줄일 때: 실적 숫자를 확인하기 전에는 판단하지 마세요.",
            "하지 말 것: 미게시를 호재나 악재로 해석.",
        ]
    )
    send(cfg, "[DSSOTP] 000150 3분기 잠정실적 미게시 | 직접 확인", body, 3, dry)
    if not dry:
        state["empty_notified"] = True
    save(state, now, dry)


def run(cfg: dict, state: dict, now: datetime, mode: str) -> None:
    dry = mode == "dry"
    t = cfg["trigger"]
    expire = datetime.fromisoformat(t["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    if mode == "auto":
        if now.year != int(cfg.get("year_guard") or 2026) or now > expire:
            print("dssotp outside window; silent")
            return
        if now.weekday() >= 5:
            print("dssotp weekend; silent")
            return
        if state.get("notified"):
            print("dssotp already notified; silent")
            return
    try:
        res = find_result(cfg, t["period_end"], t["since"])
    except Exception as exc:
        fail_once(state, cfg, now, f"공시 조회 실패: {exc}", dry)
        save(state, now, dry)
        return
    print("q3 notice:", res)
    if res is None and mode == "dry":
        print("dssotp q3 not published yet (dry); auto would stay silent")
        return
    if res is None and mode == "auto":
        if now.strftime("%Y-%m-%d") >= t["expire_date"]:
            empty_alert(cfg, state, now, dry)
        else:
            print("dssotp q3 not published yet; silent")
        return
    result_alert(cfg, state, now, res, dry)


def selftest(cfg: dict) -> None:
    got = find_result(cfg, "2026-06-30", "2026-07-01")
    print("selftest Q2 parse:", got)
    pv = cfg["trigger"]["prev"]
    ok = bool(got) and got["op"] == pv["op_eok"] and got.get("sep_op") == pv["sep_op_eok"]
    print("selftest", "OK" if ok else "MISMATCH")


def install(cfg: dict, now: datetime) -> None:
    try:
        vlines, sm = valuation_block(cfg, now)
        snap = vlines[0] if vlines else ""
        if sm.get("base"):
            snap += f" 오늘 기준칸 약 {man(sm['base'])}만원, 보수칸 약 {man(sm['cons'])}만원."
    except Exception as exc:
        snap = f"미리보기 계산 실패({exc.__class__.__name__})."
    body = "\n".join(
        [
            "이게 뭐 알림: 두산(000150) 3분기 실적 후 SOTP 민감도 알림이 설치됐다는 확인입니다.",
            f"지금 결과/상태: 평일 15:40에 3분기 연결 잠정실적 공시를 확인하고, 처음 확인된 날 한 번 대만 3사 배수와 5x5 표(배수 12~20배 x 할인 40~80%)를 보냅니다. 미리보기: {snap}",
            "지금 할 일: 없음. 매매 신호가 아닙니다.",
            "나중에: 공시가 뜨면 [DSSOTP] 제목으로 옵니다. 그 전엔 조용합니다.",
            "익절/줄일 때: 해당 없음.",
            "하지 말 것: 이 알림을 매매 신호로 해석.",
        ]
    )
    send(cfg, "[DSSOTP] 000150 설치 확인 | 매매 신호 아님", body, 3, False)


VALID_ARGS = {"auto", "dry", "force", "install"}


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    # Only these arguments exist (flow-alerts.yml runs it with none = auto). Anything else, e.g. "selftest" or a
    # typo, must never fall through to auto (audit 2026-11-02).
    unknown = [a for a in args if a not in VALID_ARGS]
    if unknown:
        print(f"dssotp: unknown argument(s) {' '.join(unknown)}; valid: (none)=auto, {', '.join(sorted(VALID_ARGS))}. "
              "There is no offline selftest mode; dry runs the live parse check without ntfy or state.", file=sys.stderr)
        return 2
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    state = load_json(STATE_PATH, DEFAULT_STATE)
    if "install" in args:
        install(cfg, now)
        return 0
    mode = "force" if "force" in args else ("dry" if "dry" in args else "auto")
    print(f"{now.isoformat()} mode={mode}")
    if mode == "dry":
        selftest(cfg)
    run(cfg, state, now, mode)
    if mode == "dry":
        print("--- table preview (dry) ---")
        result_alert(cfg, state, now, None, True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
