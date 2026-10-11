#!/usr/bin/env python3
"""Korea Zinc (010130) Q3 2026 one-shot: compare actual vs estimates, scenarios.

After results, score OP vs Meritz/Hyundai estimates, note metal sensitivity,
US project financing disclosure. Not investment advice.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "kzq3.json"
STATE = ROOT / "state" / "kzq3.json"
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}


def fetch(url):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore"), resp.geturl()


def textify(html):
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t))


def load_state():
    st = {"final_notified": False, "empty_notified": False, "fail_notified": False}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st, now):
    st["updated_at"] = now.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect(cfg):
    blobs, errors = [], []
    try:
        raw, _ = fetch(cfg["sources"]["news"])
        blobs.append(textify(raw))
    except Exception as exc:
        errors.append(f"news: {exc}")
    return "\n".join(blobs), "; ".join(errors)


def parse_op(text):
    # Look for 3분기 영업이익 숫자 (억원)
    m = re.search(r"3분기[^분]{0,40}영업이익[^억]{0,20}([0-9,]+)\s*억", text)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(r"영업이익[^억]{0,30}([0-9,]+)\s*억[^원]{0,10}3분기", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def score(op, b):
    if op is None:
        return "확인불가", "실적 원문에서 영업이익 추출 실패"
    meritz, hyundai = b["meritz_op_eok"], b["hyundai_op_eok"]
    if op >= 4300:
        return "상향", f"OP {op}억원 >= 4300, 추정 상회"
    elif op >= hyundai:
        return "기준", f"OP {op}억원, 현대차 {hyundai} 부근"
    elif op >= meritz:
        return "중립", f"OP {op}억원, 메리츠 {meritz} 상회 현대차 하회"
    else:
        return "하향", f"OP {op}억원 < 메리츠 {meritz}"


def emit(cfg, title, lines, pri):
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title[:180],
        "\n".join(lines),
        pri,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    in_win = start <= now <= expire and now.year == int(cfg.get("event_year", 2026))
    print(f"{now.isoformat()} mode={mode} window={in_win}")
    if mode not in {"force", "dry"} and not in_win:
        print("outside 2026 window; silent")
        save_state(st, now)
        return 0
    if st.get("final_notified") and mode not in {"force", "dry"}:
        print("already final; skip")
        save_state(st, now)
        return 0
    text, err = collect(cfg)
    op = parse_op(text) if text else None
    b = cfg["baseline"]
    action, why = score(op, b)
    if mode == "dry":
        # dry: print the parse/score result only. No ntfy send, no state write.
        print("DRY RUN | no ntfy, no state write")
        print(f"collect_error={err or '-'} text_chars={len(text or '')}")
        print(f"parsed_op_eok={op}")
        print(f"decision={action} | {why}")
        print(f"state(read-only)={json.dumps(st, ensure_ascii=False)}")
        if op is None:
            print("would send: [KZ] 원문 없음 | 보유 유지" + (" (skipped: empty_notified already true)" if st.get("empty_notified") else ""))
        else:
            print(f"would send: [KZ] {action} | 3분기 vs 추정")
        return 0
    if op is None and mode != "force":
        if not st.get("empty_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | 고려아연 | 3분기",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 실적 원문 미확인. 추정 비교 보류.",
                "지금 할 일: 보유 유지. DART/공시 직접 확인.",
                "나중에: 11/6, 11/7 09:10 KST 재확인. 만료 2026-11-07.",
                "익절/줄일 때: 미발표로 비중 조정 금지.",
                "하지 말 것: 추정을 실적으로 읽기. 신용, 미수, 추격매수. 투자 권유 아님.",
            ]
            emit(cfg, "[KZ] 원문 없음 | 보유 유지", lines, 3)
            st["empty_notified"] = True
            save_state(st, now)
        return 0
    # Scenario notes from prior analysis
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | 고려아연 | 3분기 비교",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 상태: 결론 {action}. {why}. 메리츠 {b['meritz_op_eok']}억, 현대차 {b['hyundai_op_eok']}억 대비. 아연 {b['zinc_lme']}달러, 은 {b['silver_spot']}달러 기준. 주가 앵커 {b['price_anchor']//10000}만원(10/8). 목표 {b['target_hyundai']//10000}만원.",
        "지금 할 일: " + ("매수 우위 검토" if action == "상향" else "중립 유지, 조정 시 분할" if action in {"기준", "중립"} else "축소 검토") + ". 미국 47억달러 조달 공개 여부 별도 확인.",
        "나중에: 은 온스당 1달러 → 연간 OP ~100~120억 변동. 아연 3,500달러 하회 시 하향. 4분기 5,000억대 회복 시 상향.",
        "익절/줄일 때: OP 3,800억 미만 또는 금속가격 급락 시 비중 축소. 인허가·조달 지연 시 할인 확대.",
        "하지 말 것: TTM PER 편차로 과평가 단정. 경영권 분쟁 무시. 신용, 미수, 추격매수. 투자 권유 아님.",
        "출처: " + cfg["sources"]["news"],
    ]
    title = f"[KZ] {action} | 3분기 vs 추정"
    emit(cfg, title, lines, 4 if action != "중립" else 3)
    st["final_notified"] = True
    st["score"] = action
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
