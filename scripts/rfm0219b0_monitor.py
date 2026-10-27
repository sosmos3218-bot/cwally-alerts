#!/usr/bin/env python3
"""[0219B0] RF머트리얼즈 3Q 실적 뒤 0219B0 재평가 요청 + 우리로 중국 계약 리스크 점검. 한 번만.

19:00 k203780 체인에 부착. 2026-11-18 ~ 11-20 KST 에만 동작(밖이면 조용히 종료). state.sent 면 다시 안 보냄.
- RF머트리얼즈(327260): KIND 에서 2026-10-01 이후 제출된 3분기 잠정실적 공정공시, 분기보고서(2026.09)를 찾아
  매출, 영업이익, 순이익과 전기/전년동기 대비를 읽음. 찾으면 그날 1번 발송. 11/20 까지 못 찾으면 대체 알림 1번.
- 우리로(046970): seed 이후 새 KIND 공시 중 단일판매/공급계약 체결, 정정, 해지 또는 ETERN, 브이선라이트, 중국 언급 요약.
  해지나 하향(또는 방향 판별 못 한) 정정이 있으면 p4.
- 읽기 실패: 알림은 보내고 값은 수동 확인 필요로 표시, cwally-ops 로 1번 안내.
modes: auto | dry(발송 없음, state 안 씀) | seed(우리로 현재 공시를 seen 으로 기록) ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import k079900_monitor as k79  # noqa: E402  (읽기 전용 재사용: http, text_of, kind_doc_text, parse_is)
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = k79.KST
NA = "확인 불가"
TAG = "[0219B0]"
CFG_PATH = ROOT / "configs" / "rfm0219b0.json"
STATE_PATH = ROOT / "state" / "rfm0219b0.json"
VIEW = k79.KIND_VIEW


def clean(s: str) -> str:
    """공시 제목의 가운뎃점류(U+318D, U+00B7 등)를 쉼표로."""
    return re.sub(r"\s*[\u318D\u00B7\u2027\u30FB]\s*", ", ", s).replace("\u2014", ", ")


def kind_all(code: str, since: str, until: str) -> list[dict]:
    form = {"method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
            "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + code, "allRepIsuSrtCd": "", "oldSearchCorpName": "",
            "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "", "searchCorpName": code, "business": "",
            "marketType": "", "settlementMonth": "", "securities": "", "submitOblgNm": "", "enterprise": "", "fromDate": since,
            "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on"}
    page = k79.http(k79.KIND + "/disclosure/details.do", data=form,
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": k79.KIND + "/disclosure/details.do?method=searchDetailsMain"})
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        a = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not a:
            continue
        rows += 1
        t = k79.text_of(row)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
        title = t[dm.end():].strip() if dm else t
        title = re.sub(r"\s*공시차트\s*주가차트\s*$", "", title)
        out.append({"acptno": a.group(1), "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "", "title": title})
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return sorted(out, key=lambda r: r["acptno"])


# ---------- RF머트리얼즈 3Q ----------
ROW = r"{name}\s*당해실적\s*([\-\d,.]+)\s+([\-\d,.]+)\s+([\-\d,.]+|-)\s+\S+\s+([\-\d,.]+)\s+([\-\d,.]+|-)"


def fnum(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_prelim(text: str) -> dict | None:
    t = re.sub(r"\s+", " ", text)
    per = re.search(r"당기실적\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", t)
    unit = re.search(r"단위\s*:\s*(백만원|억원|천원|원)", t)
    if not (per and unit):
        return None
    out = {"start": per.group(1), "end": per.group(2), "unit": unit.group(1)}
    for key, name in (("rev", "매출액"), ("op", "영업이익"), ("net", "당기순이익")):
        m = re.search(ROW.format(name=name), t)
        if not m:
            if key == "net":
                out[key] = None
                continue
            return None
        out[key] = {"cur": fnum(m.group(1)), "prev": fnum(m.group(2)), "qoq": fnum(m.group(3)), "yoy_base": fnum(m.group(4)), "yoy": fnum(m.group(5))}
    return out


def to_eok(v: float | None, unit: str) -> float | None:
    if v is None:
        return None
    return v * {"원": 1e-8, "천원": 1e-5, "백만원": 1e-2, "억원": 1.0}[unit]


def pct(v: float | None) -> str:
    return f"{v:+.1f}%" if v is not None else NA


def fmt_eok(v: float | None) -> str:
    return f"{v:,.1f}억원" if v is not None else NA


def prelim_line(p: dict) -> str:
    parts = []
    for key, name in (("rev", "매출"), ("op", "영업이익"), ("net", "순이익")):
        r = p.get(key)
        if not r:
            parts.append(f"{name} {NA}")
            continue
        parts.append(f"{name} {fmt_eok(to_eok(r['cur'], p['unit']))} (전분기 대비 {pct(r['qoq'])}, 전년동기 대비 {pct(r['yoy'])})")
    return ", ".join(parts)


def find_q3(cfg: dict, now: datetime) -> dict:
    """{prelim: (rep, parsed|None, err), report: (rep, parsed|None, err)} 찾은 것만."""
    q = cfg["q3"]
    reps = kind_all(cfg["rf"]["code"], q["search_from"], now.strftime("%Y-%m-%d"))
    found: dict = {}
    for r in reps:
        t = r["title"]
        if "잠정" in t and "실적" in t and "영업" in t:
            try:
                p = parse_prelim(k79.kind_doc_text(r["acptno"]))
                if p and (p["start"], p["end"]) == (q["period_start"], q["period_end"]):
                    found["prelim"] = (r, p, None)  # 정정이 있으면 나중 접수가 남음
                elif p is None:
                    found.setdefault("prelim_fail", (r, None, "잠정실적 표 읽기 실패"))
            except Exception as exc:  # noqa: BLE001
                found.setdefault("prelim_fail", (r, None, str(exc)[:120]))
        elif re.search(r"분기보고서", t) and f"({q['report_period']})" in t:
            try:
                s = k79.parse_is(k79.kind_doc_text(r["acptno"]))
                if not s["interim"] or len(s["rev"]) < 4:
                    raise RuntimeError("3개월 열 없음")
                found["report"] = (r, s, None)
            except Exception as exc:  # noqa: BLE001
                found["report"] = (r, None, str(exc)[:120])
    if "prelim" in found:
        found.pop("prelim_fail", None)
    return found


# ---------- 우리로 ----------
CONTRACT_RE = re.compile(r"단일판매\s*[\u318D\u00B7,]?\s*공급계약|공급계약")
KEY_RE = re.compile(r"ETERN|이턴|브이선라이트|Vsunlight|VSunlight|중국", re.I)


def amount_change(text: str) -> str | None:
    """정정 공시의 계약금액 정정전 -> 정정후. 'down' | 'up' | None."""
    t = re.sub(r"\s+", " ", text)
    m = re.search(r"계약금액[^0-9]{0,40}?([\d,]{7,})\s+([\d,]{7,})", t)
    if not m:
        return None
    a, b = fnum(m.group(1)), fnum(m.group(2))
    if a is None or b is None or a == b:
        return None
    return "down" if b < a else "up"


def wooriro_check(cfg: dict, st: dict, now: datetime) -> tuple[list[str], bool, list[str]]:
    """(요약 줄들, p4 여부, 링크들). seen 이후 새 공시만."""
    seen = set(st.get("wooriro_seen") or [])
    reps = kind_all(cfg["wooriro"]["code"], cfg["wooriro_baseline"]["seeded_at"], now.strftime("%Y-%m-%d"))
    lines, links, severe = [], [], False
    for r in reps:
        if r["acptno"] in seen:
            continue
        t = r["title"]
        is_contract = bool(CONTRACT_RE.search(t))
        body = ""
        if is_contract or "중국" in t:
            try:
                body = k79.kind_doc_text(r["acptno"])
            except Exception as exc:  # noqa: BLE001
                body = f"(본문 {NA}: {str(exc)[:60]})"
        if not (is_contract or KEY_RE.search(t) or KEY_RE.search(body)):
            continue
        tag = []
        if "해지" in t:
            tag.append("해지")
            severe = True
        if "정정" in t:
            d = amount_change(body)
            tag.append({"down": "하향 정정", "up": "상향 정정"}.get(d or "", "정정(방향 원문 확인)"))
            if d != "up":
                severe = True
        party = re.search(r"계약상대방?\s*(.{0,80}?)\s*-\s*최근", body)
        who = party.group(1).strip() if party else ""
        hit = ", ".join(sorted({m.group(0) for m in KEY_RE.finditer(t + " " + body)})) or "없음"
        lines.append(f"{r['filed']} {clean(t)}{' [' + ', '.join(tag) + ']' if tag else ''}"
                     f"{' 상대방 ' + clean(who) if who else ''}, 키워드 {hit}")
        links.append(VIEW.format(acpt=r["acptno"]))
    return lines, severe, links


# ---------- 메시지 ----------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 0219B0 | RF머트리얼즈 3Q, 우리로 중국 계약", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def baseline_line(cfg: dict) -> str:
    out = []
    for c in cfg["wooriro_baseline"]["contracts"]:
        out.append(f"{c['date']} {c['party']} {c['amount_krw'] / 1e8:,.1f}억원(USD {c['amount_usd']:,}, 매출 대비 {c['sales_pct']}%, {c['region']}, 종료 {c['end']})")
    return "기준 계약: " + "; ".join(out)


def build(cfg: dict, stamp: str, q3: dict | None, q3_err: str | None, w: tuple | None, w_err: str | None, fallback: bool):
    rf = cfg["rf"]
    links = []
    if fallback:
        title = f"{TAG} RF머트리얼즈 3Q 실적 미확인, 수동 재평가 필요"
        rf_txt = f"RF머트리얼즈({rf['code']}) 3분기 실적 공시를 KIND 에서 찾지 못했습니다{(' (' + q3_err + ')') if q3_err else ''}. 수동으로 확인하세요."
        manual, opsneed = True, bool(q3_err)
    else:
        title = f"{TAG} RF머트리얼즈 3Q 실적, 재평가 필요"
        parts, manual, opsneed = [], False, False
        if "prelim" in q3:
            r, p, _ = q3["prelim"]
            parts.append(f"잠정실적({r['filed']}): {prelim_line(p)}")
            links.append(VIEW.format(acpt=r["acptno"]))
        elif "prelim_fail" in q3:
            r, _, e = q3["prelim_fail"]
            parts.append(f"잠정실적({r['filed']}) 값 수동 확인 필요 ({e})")
            links.append(VIEW.format(acpt=r["acptno"]))
            manual = opsneed = True
        if "report" in q3:
            r, s, e = q3["report"]
            if s:
                yoy_r = (s["rev"][0] / s["rev"][2] - 1) * 100 if s["rev"][2] else None
                yoy_o = (s["op"][0] / s["op"][2] - 1) * 100 if s["op"][2] > 0 else None
                parts.append(f"분기보고서({r['filed']}, {s['basis']} 3개월): 매출 {fmt_eok(s['rev'][0])} (전년동기 대비 {pct(yoy_r)}), "
                             f"영업이익 {fmt_eok(s['op'][0])} (전년동기 대비 {pct(yoy_o)}), 순이익은 원문 확인")
            else:
                parts.append(f"분기보고서({r['filed']}) 값 수동 확인 필요 ({e})")
                manual = opsneed = True
            links.append(VIEW.format(acpt=r["acptno"]))
        rf_txt = f"RF머트리얼즈({rf['code']}) 3분기: " + " / ".join(parts)
    if w is None:
        w_txt, severe = f"우리로({cfg['wooriro']['code']}) 새 공시 목록 {NA} ({w_err}). 수동으로 확인하세요.", False
        manual = opsneed = True
    else:
        lines, severe, wl = w
        links += wl
        w_txt = (f"우리로({cfg['wooriro']['code']}) seed({cfg['wooriro_baseline']['seeded_at']}) 이후 새 계약 관련 공시: "
                 + ("; ".join(lines) if lines else "없음(새 단일판매, 공급계약 체결, 정정, 해지 공시 없음)"))
    w_txt += ". " + baseline_line(cfg)
    pri = 4 if severe else 3
    status = "\n".join([rf_txt, "0219B0 재평가 필요", f"우리로 중국 계약 리스크 점검: {w_txt}",
                        "KIND 링크: " + (", ".join(links) if links else "없음")])
    msg = body(stamp, f"{cfg['etf']['name']}(0219B0) 상위 편입사 RF머트리얼즈 3분기 실적 뒤 재평가 요청과 우리로 중국 계약(ETERN, 브이선라이트) 점검. 한 번만 옵니다. 매매 지시 아님.",
               status, "RF머트리얼즈 실적과 우리로 계약 변화를 보고 0219B0 비중 계획을 다시 정하기." + (" 값 일부는 원문으로 직접 확인." if manual else ""),
               "이 알림은 다시 오지 않습니다. 3분기 트리거 대조는 [OPTQ3] 알림도 참고.",
               "계약 해지나 하향 정정이 있으면 비중 축소부터 검토." if severe else "실적 한 분기로 비중을 크게 바꾸지 않기.",
               "자동 요약만 보고 판단 끝내기, 확인 불가 값을 정상으로 읽기.")
    return title, msg, pri, opsneed


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 공시 파싱 실패 | 원문 확인 필요",
            body(stamp, "0219B0 재평가 알림의 데이터 읽기 실패 안내. 매매 신호 아님.", status, "KIND 원문을 직접 보세요.",
                 "재평가 알림은 이미 보냈거나 대체 알림으로 갑니다.", "이 안내로 비중을 바꾸지 않기.", "읽기 실패를 실적 부진으로 읽기."))


# ---------- 실행 ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(mode: str, now: datetime, cfg: dict, pub=publish) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    st = load_state()
    if mode == "seed":
        reps = kind_all(cfg["wooriro"]["code"], "2025-01-01", today)
        st["wooriro_seen"] = sorted({r["acptno"] for r in reps} | set(st.get("wooriro_seen") or []))
        st["seeded_at"] = stamp
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"seeded {len(st['wooriro_seen'])} 우리로 filings")
        return 0
    w0, w1 = cfg["window"]["start"], cfg["window"]["end"]
    if not (w0 <= today <= w1):
        print(f"{today} 창 밖({w0} ~ {w1}): 조용히 종료")
        return 0
    if st.get("sent"):
        print(f"already sent {st['sent']}: 조용히 종료")
        return 0
    q3, q3_err = None, None
    try:
        q3 = find_q3(cfg, now)
    except Exception as exc:  # noqa: BLE001
        q3_err = f"KIND 목록 {NA}: {str(exc)[:120]}"
    have = bool(q3) and any(k in q3 for k in ("prelim", "prelim_fail", "report"))
    print(f"q3 found: {sorted(q3) if q3 else q3_err}")
    if not have and today < w1:
        print("RF 3분기 공시 아직 못 찾음, 마지막 날(11/20) 전이라 조용히 종료")
        return 0
    w, w_err = None, None
    try:
        w = wooriro_check(cfg, st, now)
    except Exception as exc:  # noqa: BLE001
        w_err = str(exc)[:120]
    title, msg, pri, opsneed = build(cfg, stamp, q3 if have else None, q3_err, w, w_err, fallback=not have)
    assert "\u00b7" not in title + msg and "\u2014" not in title + msg, title
    assert "확인 불가" not in title and "조회 실패" not in title
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
    ops = None
    if opsneed:
        ops = ops_msg(stamp, "RF머트리얼즈 3분기 값이나 우리로 공시 목록 일부를 자동으로 읽지 못했습니다. 재평가 알림 본문의 수동 확인 항목을 보세요.")
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    pub(topic, title, msg, pri, ["mag"], server, config_name="rfm0219b0")
    st["sent"] = {"at": stamp, "title": title, "priority": pri, "fallback": not have}
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name="rfm0219b0")
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode not in {"auto", "dry", "seed"}:
        print("usage: rfm0219b0_monitor.py <auto|dry|seed> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")))
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
