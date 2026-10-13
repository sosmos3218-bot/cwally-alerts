#!/usr/bin/env python3
"""[SOILQ3] S-Oil 3Q26 preliminary earnings watch on the weekday 09:00 morning slot. Not a trade order.

Sends once when the IR (earnings date) notice appears, and once when the Q3 preliminary results notice appears
(operating profit vs the Naver consensus snapshot). Silent otherwise.
Modes: auto (default), dry (no send, no state write; SOILQ3_TEST_NOTICE=<json file> overrides the notice list),
install (one install-confirmation send, no state write), force (ignore window).
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "soilq3.json"
STATE_PATH = ROOT / "state" / "soilq3.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts soilq3)"}


def fetch(url: str) -> str:
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise last if last else RuntimeError("fetch failed")


def text_of(contents: str) -> str:
    b = re.sub(r"(?is)<style.*?</style>", "", contents or "")
    t = html.unescape(re.sub(r"[ \t\r\n]+", " ", re.sub(r"<[^>]+>", " | ", b)))
    return re.sub(r"(\|\s*)+", "| ", t)


def to_num(s: str) -> float | None:
    s = s.strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_results(t: str) -> dict:
    """Return 백만원 values of the current quarter (당해실적 first column) and the prior quarter."""
    period = re.search(r"당기실적 \| (\d{4}-\d{2}-\d{2}) \| ~ \| (\d{4}-\d{2}-\d{2})", t)
    out: dict = {"period": period.groups() if period else None}
    for key, label in (("sales", "매출액"), ("op", "영업이익"), ("ni", "당기순이익")):
        m = re.search(re.escape(label) + r" \| 당해실적 \| ([-\d,\.]+) \| ([-\d,\.]+)", t)
        if m:
            out[key] = to_num(m.group(1))
            out[key + "_prev"] = to_num(m.group(2))
    call = re.search(r"정보제공\(예정\)일자 \| ([^|]+)\| 정보제공\(예정\)시간 \| ([^|]+)\|", t)
    out["call"] = " ".join(x.strip() for x in call.groups()) if call else None
    return out


def parse_ir(t: str) -> str | None:
    m = re.search(r"(?:일시|일자)[^|]*\| ([^|]*\d{4}[^|]*)\|", t)
    if m:
        return m.group(1).strip()
    m = re.search(r"(\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일[^|]{0,20})", t)
    return m.group(1).strip() if m else None


def consensus_op(cfg: dict) -> float | None:
    """억원."""
    d = json.loads(fetch(cfg["sources"]["consensus"]))
    fi = d.get("financeInfo") or d
    titles = {t.get("key"): t.get("isConsensus") for t in fi.get("trTitleList", [])}
    if titles.get(cfg["consensus_key"]) != "Y":
        return None
    for row in fi.get("rowList", []):
        if row.get("title") == "영업이익":
            v = (row.get("columns", {}).get(cfg["consensus_key"]) or {}).get("value")
            return to_num(v) if v else None
    return None


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | SOILQ3 | S-Oil 3분기 잠정실적",
            "이게 뭐 알림: S-Oil 3분기 실적 발표일 공시와 잠정실적 공시가 새로 나왔을 때만 울립니다. 매매 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def eok(v_million: float) -> str:
    return f"{v_million / 100:,.0f}억원"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry, install = mode == "dry", mode == "install"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    except json.JSONDecodeError:
        state = {}
    now = datetime.now(KST)
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if mode not in {"force", "dry", "install"} and not (cfg["window_start"] <= today <= cfg["valid_until"]):
        print("outside window; silent")
        return 0

    alerts: list[tuple[str, str, int]] = []
    if install:
        cons = None
        try:
            cons = consensus_op(cfg)
        except Exception as exc:
            print(f"consensus err {exc}")
        cs = f"네이버 3분기 영업이익 컨센서스 {cons:,.0f}억원." if cons else "컨센서스 조회 안 됨."
        alerts.append(
            (
                "[SOILQ3] 설치 확인 | 매매 신호 아님",
                body(
                    stamp,
                    f"설치 확인 발송입니다. 발표일은 아직 공시 전이고 {cfg['expected_date_note']}. {cs}",
                    "할 일 없음. 이 알림이 왔으면 설치가 된 것입니다.",
                    "평일 09:00에 S-Oil 공시를 봅니다. 실적 발표일(IR) 공시와 3분기 잠정실적 공시가 나오면 각각 한 번씩 울립니다.",
                    "해당 없음.",
                    "이 설치 확인 알림을 매매 신호로 보기.",
                ),
                2,
            )
        )
    else:
        notices = None
        try:
            if dry and os.environ.get("SOILQ3_TEST_NOTICE"):
                notices = json.loads(Path(os.environ["SOILQ3_TEST_NOTICE"]).read_text(encoding="utf-8"))
            else:
                notices = json.loads(fetch(cfg["sources"]["notice"]))
            if not isinstance(notices, list):
                raise RuntimeError("notice list format changed")
        except Exception as exc:
            if state.get("fail_date") != today:
                alerts.append(
                    (
                        "[SOILQ3] 공시 조회 실패 | 조건 미확인",
                        body(stamp, f"S-Oil 공시 목록 조회 실패. {exc}. 실적 미발표라는 뜻이 아닙니다.", "DART나 증권 앱에서 S-Oil 공시를 직접 보세요.", "다음 평일 09:00 KST에 재시도합니다.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 미발표로 취급."),
                        3,
                    )
                )
                state["fail_date"] = today
            notices = []

        # Snapshot consensus before release (Naver may replace it with actuals afterwards).
        if not state.get("results_sent"):
            try:
                c = consensus_op(cfg)
                if c:
                    state["consensus_op_eok"] = c
                    state["consensus_date"] = today
            except Exception as exc:
                print(f"consensus err {exc}")

        since = cfg["quarter_start"]
        for n in sorted(notices, key=lambda x: x.get("datetime", "")):
            title = n.get("title", "")
            dt = str(n.get("datetime", ""))
            if dt[:10] < since:
                continue
            nid = str(n.get("no"))
            if "기업설명회" in title and not state.get("ir_sent") and dt[:10] >= "2026-10-01":
                when = parse_ir(text_of(n.get("contents", ""))) or "본문에서 일시 확인 필요"
                alerts.append(
                    (
                        "[SOILQ3] 실적 발표일 공시 | 날짜만 확인",
                        body(stamp, f"S-Oil 기업설명회(IR) 개최 공시({dt[:16].replace('T', ' ')}). 일시: {when}.", "발표일을 캘린더에 적어 두세요. 아직 실적 숫자는 아닙니다.", "잠정실적 공시가 나오면 컨센서스 비교와 함께 다시 울립니다.", "해당 없음.", "발표 전 기대만으로 추가 매수."),
                        2,
                    )
                )
                state["ir_sent"] = nid
            if "잠정" in title and "실적" in title and not state.get("results_sent"):
                r = parse_results(text_of(n.get("contents", "")))
                if not r.get("period") or r["period"][0] != since:
                    continue
                op = r.get("op")
                if op is None:
                    alerts.append(
                        (
                            "[SOILQ3] 실적 파싱 실패 | 원문 확인",
                            body(stamp, f"3분기 잠정실적 공시({dt[:16].replace('T', ' ')})는 나왔지만 숫자를 읽지 못했습니다.", "DART에서 S-Oil 잠정실적 공시 원문을 직접 보세요.", "자동 재시도는 없습니다.", "숫자 확인 전 비중 변경 보류.", "파싱 실패를 실적 부진으로 취급."),
                            3,
                        )
                    )
                    state["results_sent"] = nid
                    continue
                cons = state.get("consensus_op_eok")
                op_eok = op / 100
                band = float(cfg["grade_band_pct"])
                if cons:
                    gap = (op_eok / cons - 1) * 100
                    grade = "상회" if gap >= band else ("하회" if gap <= -band else "부합")
                    cs = f"컨센서스 {cons:,.0f}억원({state.get('consensus_date')} 네이버) 대비 {gap:+.1f}%, {grade}."
                else:
                    grade, cs = "비교불가", "컨센서스 스냅샷이 없어 비교하지 못했습니다."
                prev = r.get("op_prev")
                qoq = f" 2분기 {eok(prev)} 대비 {((op / prev - 1) * 100):+.1f}%." if prev else ""
                status = f"3분기 영업이익 {eok(op)}, 매출 {eok(r['sales']) if r.get('sales') else '미확인'}, 순이익 {eok(r['ni']) if r.get('ni') else '미확인'}. {cs}{qoq}"
                if r.get("call"):
                    status += f" 컨퍼런스콜 {r['call']}."
                acts = {
                    "상회": ("보유 중이면 유지. 컨퍼런스콜의 4분기 정제마진, 재고평가 코멘트를 확인하세요.", "이미 1년 새 크게 오른 종목이라 실적만 보고 추격 매수하지 마세요."),
                    "부합": ("보유 유지. 4분기 가이던스와 [OIL] 정제마진 추정치 방향을 같이 보세요.", "부합 실적을 매수 신호로 해석."),
                    "하회": ("비중이 있으면 축소를 검토하세요. 재고평가손실 같은 일회성인지 컨퍼런스콜에서 먼저 확인하세요.", "일회성 확인 없이 전량 매도."),
                    "비교불가": ("숫자를 증권사 컨센서스와 직접 비교하세요.", "비교 없이 판단."),
                }[grade]
                alerts.append(
                    (
                        f"[SOILQ3] 3분기 영업이익 {grade} | {'추격 금지' if grade == '상회' else ('비중 점검' if grade == '하회' else '유지 점검')}",
                        body(stamp, status, acts[0], "4분기는 정제마진과 유가 방향이 좌우합니다. [OIL] 알림(정제마진 15달러, 브렌트 85/120)을 같이 보세요.", "[OIL] 정제마진 추정치가 15달러 아래로 가면 S-Oil부터 줄일 후보입니다.", acts[1]),
                        4,
                    )
                )
                state["results_sent"] = nid

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["fuelpump"], server)
    if not alerts:
        print("no alert")
    if dry or install:
        print("state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
