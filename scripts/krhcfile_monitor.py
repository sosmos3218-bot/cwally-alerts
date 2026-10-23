#!/usr/bin/env python3
"""[KR-HC-RPT] 셀트리온 (068270) / 삼성바이오로직스 (207940) 정기보고서 제출 알림 (이벤트만).

k079900 정기보고서 부분과 같은 방식 (함수는 k079900_monitor 에서 읽기 전용으로 재사용):
  - 목록: KIND 회사별 공시 검색. 이 두 회사는 공시가 많아 '보고서' 한 번 검색(100건)으로는 오래된 정기보고서가
    잘리므로 분기/반기/사업보고서를 종류별로 따로 검색한다.
  - 본문: KIND 공시뷰어 원문의 연결(없으면 별도) 포괄손익계산서. 행 이름 뒤 주석 번호 '(주4,21)' 는 지우고 읽는다.
  - 분기 단독: 분기/반기 보고서는 3개월 열, 사업보고서는 연간 - 같은 해 3분기 누적.
  - 비교: 전년 같은 분기, 직전 분기의 매출, 영업이익, 영업이익률(차이 %p). 판정 라벨 없음.
    전년 같은 분기는 이번 보고서 안의 전기 열(재작성 기준 그대로)을 쓴다. 다른 보고서 숫자를 섞는 계산은
    두 보고서가 같은 기준인지(겹치는 기간 숫자가 match_tol_pct 안에서 같은지) 확인하고, 다르면 확인 불가.
  - 링크: DART 공시검색 rcpNo, 못 찾으면 KIND 뷰어 링크. 중복 방지: KIND 접수번호. 첫 실행은 기존 보고서 시드.
modes: auto(기본) | dry(발송 없음, state 안 씀) | parse(회사별 최신 보고서를 읽어 메시지 견본만 출력, 발송/state 없음)
매매 신호가 아니다.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
import k079900_monitor as k79  # noqa: E402  (읽기 전용 재사용: http, text_of, kind_doc_text, parse_is, dart_link)

KST = k79.KST
NA = k79.NA
NAME = "krhcfile"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
NOTE_RE = re.compile(r"\(\s*주\s*[\d,\s\.]*\)\s*")


def load_state() -> dict:
    base = {"updated_at": None, "seeded": {}, "seen_acptno": [], "fail_notice": {}}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


# ---------------- KIND ----------------
def kind_list(co: dict, since: str, until: str) -> list[dict]:
    out: dict[str, dict] = {}
    for nm in ("분기보고서", "반기보고서", "사업보고서"):
        form = {
            "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
            "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + co["code"], "allRepIsuSrtCd": "",
            "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": nm, "reportCd": "",
            "searchCorpName": co["code"], "business": "", "marketType": "", "settlementMonth": "", "securities": "",
            "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
        }
        page = k79.http(k79.KIND + "/disclosure/details.do", data=form,
                        headers={"X-Requested-With": "XMLHttpRequest",
                                 "Referer": k79.KIND + "/disclosure/details.do?method=searchDetailsMain"})
        seen_rows = 0
        for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
            acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
            if not acpt:
                continue
            seen_rows += 1
            t = k79.text_of(row)
            if co["name_kr"] not in t:
                continue
            m = re.search(r"((?:\[[^\]]+\]\s*)*)(분기보고서|반기보고서|사업보고서)\s*\([^)]*\)\s*\((\d{4})\.(\d{2})\)", t)
            if not m:
                continue
            dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
            out[acpt.group(1)] = {"acptno": acpt.group(1), "kind": m.group(2), "tags": m.group(1).strip(),
                                  "period": f"{m.group(3)}.{m.group(4)}",
                                  "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "", "title": m.group(0).strip()}
        if seen_rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
            raise RuntimeError(f"KIND 응답 형식 이상({nm})")
    return sorted(out.values(), key=lambda r: r["acptno"], reverse=True)


# ---------------- 분석 ----------------
class Book:
    """기간(YYYY.MM)별 최신 접수 보고서를 읽어 둔다 (같은 실행 안에서 한 번만 받음)."""

    def __init__(self, reps: list[dict], fetch=k79.kind_doc_text):
        self.by_period: dict[str, dict] = {}
        for r in sorted(reps, key=lambda r: r["acptno"]):
            self.by_period[r["period"]] = r  # 정정이 있으면 나중 접수가 남음
        self.fetch = fetch
        self.cache: dict[str, dict] = {}

    def st(self, period: str, rep: dict | None = None) -> dict:
        rep = rep or self.by_period.get(period)
        if not rep:
            raise RuntimeError(f"{period} 보고서 목록에 없음")
        if rep["acptno"] not in self.cache:
            s = k79.parse_is(NOTE_RE.sub("", self.fetch(rep["acptno"])))
            want = 4 if s["interim"] else 2
            if len(s["rev"]) < want or len(s["op"]) < want:
                raise RuntimeError(f"{period} 표 열 수 부족")
            self.cache[rep["acptno"]] = s
        return self.cache[rep["acptno"]]


def _same(a: tuple[float, float], b: tuple[float, float], tol_pct: float) -> bool:
    lim = max(1.0, abs(a[0]) * tol_pct / 100)
    return abs(a[0] - b[0]) <= lim and abs(a[1] - b[1]) <= lim


def analyze(rep: dict, book: Book, tol: float) -> dict:
    yr, mo = rep["period"].split(".")
    y = int(yr)
    qn = {"03": 1, "06": 2, "09": 3, "12": 4}[mo]
    cur_st = book.st(rep["period"], rep)
    basis = cur_st["basis"]
    notes: list[str] = []

    def chk_basis(s: dict, what: str) -> None:
        if s["basis"] != basis or not s["interim"] and what.endswith("분기"):
            raise RuntimeError(f"{what} 표 기준 다름")

    def q4_parts(year: int, ann: dict) -> tuple[dict, str]:
        """year 의 3분기 보고서와 사업보고서가 같은 기준인지 확인 (전년 연간 숫자가 전년 사업보고서와 같은지)."""
        q3 = book.st(f"{year}.09")
        if not q3["interim"] or q3["basis"] != ann["basis"]:
            raise RuntimeError(f"{year}.09 표 형식 다름")
        prev_ann = book.st(f"{year - 1}.12")
        if prev_ann["basis"] != ann["basis"] or not _same((ann["rev"][1], ann["op"][1]), (prev_ann["rev"][0], prev_ann["op"][0]), tol):
            raise RuntimeError(f"{year}년 중 재작성 가능성({year - 1}년 연간 숫자가 보고서마다 다름)")
        return q3, f"{year} 연간 - {year} 3분기 누적"

    # 이번 분기
    if qn < 4:
        if not cur_st["interim"]:
            raise RuntimeError("분기 보고서인데 3개월 열 없음")
        cur = (cur_st["rev"][0], cur_st["op"][0])
        cur_how = "3개월"
    else:
        q3, cur_how = q4_parts(y, cur_st)
        cur = (cur_st["rev"][0] - q3["rev"][1], cur_st["op"][0] - q3["op"][1])
        cur_how = "연간 - 3분기 누적"
    res = {"qname": f"{y}년 {qn}분기", "basis": basis, "cur": cur, "cur_how": cur_how, "yoy": None, "qoq": None}

    # 전년 같은 분기
    try:
        if qn < 4:
            res["yoy"] = (cur_st["rev"][2], cur_st["op"][2])
            res["yoy_how"] = "이번 보고서의 전기 3개월 열"
        else:
            q3 = book.st(f"{y}.09")
            res["yoy"] = (cur_st["rev"][1] - q3["rev"][3], cur_st["op"][1] - q3["op"][3])
            res["yoy_how"] = "전년 연간 - 전년 3분기 누적(같은 해 보고서의 전기 열)"
    except Exception as exc:  # noqa: BLE001
        notes.append(f"전년 같은 분기 {NA} ({exc})")

    # 직전 분기
    try:
        if qn == 2:
            p = book.st(f"{y}.03")
            chk_basis(p, "1분기")
            if not _same((cur_st["rev"][3] - cur_st["rev"][2], cur_st["op"][3] - cur_st["op"][2]), (p["rev"][2], p["op"][2]), tol):
                raise RuntimeError("1분기 보고서와 기준 다름(전년 1분기 숫자 불일치)")
            res["qoq"] = (p["rev"][0], p["op"][0])
            res["qoq_how"] = f"{y} 1분기 보고서 3개월 열"
        elif qn == 3:
            p = book.st(f"{y}.06")
            chk_basis(p, "반기")
            if not _same((cur_st["rev"][3] - cur_st["rev"][2], cur_st["op"][3] - cur_st["op"][2]), (p["rev"][3], p["op"][3]), tol):
                raise RuntimeError("반기 보고서와 기준 다름(전년 상반기 숫자 불일치)")
            res["qoq"] = (p["rev"][0], p["op"][0])
            res["qoq_how"] = f"{y} 반기 보고서 3개월 열"
        elif qn == 4:
            p = book.st(f"{y}.09")
            res["qoq"] = (p["rev"][0], p["op"][0])
            res["qoq_how"] = f"{y} 3분기 보고서 3개월 열"
        else:
            ann = book.st(f"{y - 1}.12")
            q3, _ = q4_parts(y - 1, ann)
            q1p = book.st(f"{y - 1}.03")
            if not _same((cur_st["rev"][2], cur_st["op"][2]), (q1p["rev"][0], q1p["op"][0]), tol):
                raise RuntimeError(f"{y - 1}년 1분기 숫자가 보고서마다 다름(재작성 가능성)")
            res["qoq"] = (ann["rev"][0] - q3["rev"][1], ann["op"][0] - q3["op"][1])
            res["qoq_how"] = f"{y - 1} 연간 - {y - 1} 3분기 누적"
    except Exception as exc:  # noqa: BLE001
        notes.append(f"직전 분기 {NA} ({exc})")
    res["notes"] = notes
    return res


def fig(v: tuple[float, float]) -> str:
    m = v[1] / v[0] * 100 if v[0] else None
    return f"매출 {v[0]:,.0f}억, 영업이익 {v[1]:,.0f}억, 영업이익률 {m:.2f}%" if m is not None else f"매출 {v[0]:,.0f}억, 영업이익 {v[1]:,.0f}억"


def cmp(cur: tuple[float, float], old: tuple[float, float]) -> str:
    def ch(a: float, b: float) -> str:
        return f"{(a / b - 1) * 100:+.1f}%" if b > 0 else NA
    s = f"매출 {ch(cur[0], old[0])}, 영업이익 {ch(cur[1], old[1])}"
    if cur[0] and old[0]:
        s += f", 영업이익률 {(cur[1] / cur[0] - old[1] / old[0]) * 100:+.2f}%p"
    return s


def report_msg(co: dict, rep: dict, res: dict | None, err: str | None, link: str, stamp: str) -> tuple[str, str, int, list[str]]:
    tag = f"{rep['tags']} " if rep["tags"] else ""
    name = f"{tag}{rep['kind']} ({rep['period']})"
    if res:
        cm = res["cur"][1] / res["cur"][0] * 100 if res["cur"][0] else None
        head = f"{res['qname']} 단독 영업이익률 {cm:.1f}%" if cm is not None else f"{res['qname']} 단독"
        if res.get("yoy") and res["yoy"][0] and res["cur"][0]:
            head += f", 전년 같은 분기 대비 {(res['cur'][1] / res['cur'][0] - res['yoy'][1] / res['yoy'][0]) * 100:+.1f}%p"
        title = f"[KR-HC-RPT] {co['name_kr']} {name} 제출 | {head}"
        parts = [f"{res['qname']} 단독({res['basis']}, {res['cur_how']}): {fig(res['cur'])}"]
        if res.get("yoy"):
            parts.append(f"전년 같은 분기({res['yoy_how']}): {fig(res['yoy'])}. 대비 {cmp(res['cur'], res['yoy'])}")
        if res.get("qoq"):
            parts.append(f"직전 분기({res['qoq_how']}): {fig(res['qoq'])}. 대비 {cmp(res['cur'], res['qoq'])}")
        parts += res["notes"]
        status = f"{name} 제출 {rep['filed']} KST. " + " / ".join(parts) + f". 링크 {link}"
    else:
        title = f"[KR-HC-RPT] {co['name_kr']} {name} 제출 | 영업이익률 자동 계산 안 됨, 원문 확인"
        status = f"{name} 제출 {rep['filed']} KST. 영업이익률 {NA} ({err or '표를 읽지 못함'}). 링크 {link}"
    msg = "\n".join([
        f"{stamp} | {co['code']} | {co['name_kr']}",
        f"이게 뭐 알림: {co['name_kr']} 분기/반기/사업보고서가 새로 나오면 1번 오는 알림. 분기 단독 매출, 영업이익, 영업이익률을 전년 같은 분기와 직전 분기에 비교. 매매 신호 아님.",
        f"지금 결과/상태: {status}",
        "지금 할 일: 원문에서 숫자를 한 번 더 확인. 일회성 항목(기타수익, 환율, 재고 조정)이 영업이익에 섞였는지 함께 보기.",
        "나중에: 다음 정기보고서가 나오면 다시 옵니다. 주간 흐름은 [KR-HC] 주간 비교에서 봅니다.",
        "익절/줄일 때: 이 숫자 하나로 정하지 않기. 미리 정한 비중 기준대로.",
        "하지 말 것: 영업이익률 한 분기 변화만 보고 사거나 팔기, 확인 불가 항목을 0이나 정상으로 읽기.",
    ])
    return title, msg, 3, ["page_facing_up"]


def link_for(co: dict, rep: dict) -> str:
    link = None
    try:
        link = k79.dart_link(co, rep)
    except Exception as exc:  # noqa: BLE001
        print(f"dart link failed: {exc}", file=sys.stderr)
    return link or f"{k79.KIND_VIEW.format(acpt=rep['acptno'])} (KIND 원문, DART 링크 못 찾음)"


def one(co: dict, rep: dict, reps: list[dict], tol: float, stamp: str, book: Book | None = None):
    res, err = None, None
    try:
        res = analyze(rep, book or Book(reps), tol)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:160]
    return report_msg(co, rep, res, err, link_for(co, rep), stamp)


# ---------------- main ----------------
def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    if mode not in {"auto", "dry", "parse"}:
        print(f"unknown mode {mode}")
        return 2
    dry = mode != "auto"
    rc = cfg["reports"]
    tol = float(rc.get("match_tol_pct", 0.5))
    since = (now - timedelta(days=int(rc["lookback_days"]))).strftime("%Y-%m-%d")
    state = load_state()
    out: list[tuple[str, str, int, list[str]]] = []

    def ops_once(key: str, title: str, msg: str) -> None:
        fn = state.setdefault("fail_notice", {})
        if fn.get(key) != today:
            out.append((title, msg, 3, ["warning"]))
            fn[key] = today

    for co in cfg["companies"]:
        try:
            reps = kind_list(co, since, today)
            print(f"{co['code']} reports:", [(r["acptno"], r["title"], r["filed"]) for r in reps])
            if mode == "parse":
                if reps:
                    t, m, p, _ = one(co, reps[0], reps, tol, stamp)
                    print(f"-- parse latest ({reps[0]['title']})\nTITLE: {t}\n{m}\n")
                continue
            seen = set(state.get("seen_acptno") or [])
            if not state.setdefault("seeded", {}).get(co["code"]):
                state["seen_acptno"] = sorted(seen | {r["acptno"] for r in reps})
                state["seeded"][co["code"]] = today
                print(f"{co['code']} seeded (no alert):", [r["acptno"] for r in reps])
                continue
            book = Book(reps)
            for rep in sorted((r for r in reps if r["acptno"] not in seen), key=lambda r: r["acptno"]):
                out.append(one(co, rep, reps, tol, stamp, book))
                state["seen_acptno"] = sorted(set(state.get("seen_acptno") or []) | {rep["acptno"]})
        except Exception as exc:  # noqa: BLE001
            print(f"{co['code']} KIND fetch failed: {exc}", file=sys.stderr)
            ops_once(f"kind_{co['code']}", f"[KR-HC-RPT] {co['name_kr']} 정기보고서 목록 조회 실패 | 공시 판정 못 함",
                     "\n".join([f"{stamp} | {co['code']} | {co['name_kr']}",
                                f"이게 뭐 알림: {co['name_kr']} 정기보고서 알림의 데이터 조회 실패 안내.",
                                f"지금 결과/상태: KIND 공시 목록 {NA} ({str(exc)[:200]}).",
                                f"지금 할 일: 필요하면 DART에서 {co['name_kr']} 공시를 직접 보세요.",
                                "나중에: 다음 실행 때 다시 봅니다.",
                                "익절/줄일 때: 이번 안내로 비중을 바꾸지 마세요.",
                                "하지 말 것: 조회 실패를 새 보고서 없음으로 읽기."]))
    if mode == "parse":
        print("PARSE: no publish, no state")
        return 0
    for title, msg, pri, tags in out:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
