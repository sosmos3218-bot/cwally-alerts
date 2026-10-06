#!/usr/bin/env python3
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
CFG_PATH = ROOT / "configs" / "sndk.json"
STATE_PATH = ROOT / "state" / "sndk.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


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


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit(cfg: dict, title: str, lines: list[str], priority: int = 3) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["chart_with_upwards_trend"], server)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def ir_headline(url: str) -> tuple[bool, str]:
    try:
        html = fetch(url)
    except Exception as exc:
        return False, str(exc)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    hit = re.search(
        r"(fiscal first quarter|Q1 FY ?2027|first quarter of fiscal 2027|Reports Fiscal First Quarter)(.{0,160})",
        text,
        re.I,
    )
    if hit:
        return True, hit.group(0)[:180].strip()
    return True, ""


def body_common(stamp: str, phase: str) -> list[str]:
    return [
        f"{stamp} | PORT | SNDK 체크 {phase}",
        "이게 뭐 알림: 샌디스크(SNDK) 1분기 가이던스 103~108억달러 점검. 매수·매도 신호가 아닙니다. 삼성전자·SK하이닉스는 스위치 3개가 켜지기 전엔 따로 봅니다.",
    ]


def kr_pre(cfg: dict, stamp: str) -> tuple[str, list[str]]:
    a = cfg["anchors"]
    title = "[SNDK] 국내 5개부터 | 연결 스위치 꺼짐"
    lines = body_common(stamp, "10/28 국내 선행")
    lines += [
        "지금 결과: 삼성·하이닉스 3분기 숫자 창. SNDK 실적은 아직 11/5 미국 장후.",
        (
            "지금 할 일: 국내 필수 5개만 채우세요. "
            f"①삼성 HBM4 매출 {a['sec_hbm4_qoq']:.0f}배 전후 "
            f"②하반기 HBM4 비중 {a['sec_hbm4_mix']:.0f}% 초과 "
            f"③웨이퍼 {a['sec_wafer_now']:.0f}만→{a['sec_wafer_next']:.0f}만장 "
            "④하이닉스 2027 HBM 계약 "
            "⑤서버 D램 가격·납기."
        ),
        "나중에: 11/5 09:00에 SNDK 5개를 보냅니다. 오늘은 연결하지 마세요.",
        "익절/줄일 때: 국내 실적만으로 SNDK를 따라가지 마세요.",
        "하지 말 것: 목표가 2,400달러를 매매 근거로 쓰기. 헤드라인 영업이익으로 스위치 켜기. 신용·미수·추격.",
    ]
    return title, lines


def sndk_pre(cfg: dict, stamp: str) -> tuple[str, list[str]]:
    a = cfg["anchors"]
    title = "[SNDK] 오늘 장후 SNDK | 연결 보류"
    lines = body_common(stamp, "11/5 실적 전")
    lines += [
        f"지금 결과: 발표는 미국 장후, KST {cfg['print_kst']} 전후. 숫자는 아직 없음.",
        (
            "지금 할 일: 밤에 볼 SNDK 5개만 준비. "
            f"①매출 {a['rev_lo']}~{a['rev_hi']}억달러 "
            f"②매출총이익률 {a['gm_lo']:.0f}~{a['gm_hi']:.0f}% "
            f"③데이터센터 매출 {a['dc_rev_q4']}억 대비 "
            f"④비트 비중 {a['dc_bit_q4']:.0f}% 대비 "
            "⑤성장이 가격인지 물량인지."
        ),
        "나중에: 11/6 09:00에 발표문 대조 알림. 발표 직후 추격 금지.",
        "익절/줄일 때: 가이던스 하회면 SNDK만 재검토. 국내 자동 매도가 아닙니다.",
        "하지 말 것: 숏츠 목표가, 컨센서스 평균으로 선매매. 신용·미수.",
    ]
    return title, lines


def sndk_post(cfg: dict, stamp: str, ir_ok: bool, headline: str) -> tuple[str, list[str]]:
    a = cfg["anchors"]
    if ir_ok and headline:
        title = "[SNDK] 발표문 열림 | 5개 대조"
        status = f"IR에서 관련 문구를 찾음. {headline}"
    elif ir_ok:
        title = "[SNDK] 숫자 직접 대조 | 연결 기본 꺼짐"
        status = "IR은 열렸으나 Q1 숫자 문구는 확인 불가. 추정하지 않음."
    else:
        title = "[SNDK] IR 조회 실패 | 직접 확인"
        status = f"IR 조회 실패. {headline or '출처 없음'}."
    lines = body_common(stamp, "11/6 실적 후")
    lines += [
        f"지금 결과: {status}",
        (
            "지금 할 일: SNDK 5개 대조. "
            f"①매출 {a['rev_lo']}~{a['rev_hi']} "
            f"②이익률 {a['gm_lo']:.0f}% 이상 "
            f"③데이터센터 {a['dc_rev_q4']}억 "
            f"④비트 {a['dc_bit_q4']:.0f}% "
            "⑤가격 vs 물량. "
            "스위치 3개: "
            f"(1)매출 {a['rev_lo']}억 이상+이익률 {a['gm_lo']:.0f}% 이상 "
            "(2)데이터센터 매출·비트가 Q4 유지/상회이고 물량도 동반 "
            "(3)같은 주 국내 서버 D램·HBM이 타이트. "
            "3개가 모두 켜질 때만 삼성·하이닉스를 같이 보세요."
        ),
        "나중에: 스위치 0~1개면 분리, 2개면 관찰, 3개면 같은 방향. 이 알림은 후속 발송 없음.",
        "익절/줄일 때: 매출만 맞고 데이터센터가 빠지면 국내 비중을 올리지 마세요.",
        f"하지 말 것: 조회 실패를 호재로 읽기. 목표가 2,400달러로 추격. 출처: {cfg['ir_url']}",
    ]
    return title, lines


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(
        STATE_PATH,
        {
            "kr_pre_notified": False,
            "sndk_pre_notified": False,
            "sndk_post_notified": False,
            "fail_notified": False,
            "last_phase": None,
            "updated_at": None,
        },
    )
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    dates = cfg["send_dates"]
    in_year = now.year == int(cfg["year_guard"])
    in_window = cfg["window_start"] <= today <= cfg["window_end"]
    print(f"{stamp} mode={mode} today={today} window={in_window}")

    if mode not in {"force", "dry"} and (not in_year or not in_window):
        print("outside 2026 window; silent")
        return 0

    phase = None
    if today == dates["kr_pre"] and (mode in {"force", "dry"} or not state.get("kr_pre_notified")):
        phase = "kr_pre"
    elif today == dates["sndk_pre"] and (mode in {"force", "dry"} or not state.get("sndk_pre_notified")):
        phase = "sndk_pre"
    elif today == dates["sndk_post"] and (mode in {"force", "dry"} or not state.get("sndk_post_notified")):
        phase = "sndk_post"
    elif mode in {"force", "dry"}:
        if today <= dates["kr_pre"]:
            phase = "kr_pre"
        elif today <= dates["sndk_pre"]:
            phase = "sndk_pre"
        else:
            phase = "sndk_post"

    if phase is None:
        print("not a send date or already sent; silent")
        return 0

    ir_ok, headline = True, ""
    if phase == "sndk_post":
        ir_ok, headline = ir_headline(cfg["ir_url"])

    if phase == "kr_pre":
        title, lines = kr_pre(cfg, stamp)
        flag = "kr_pre_notified"
        pri = 3
    elif phase == "sndk_pre":
        title, lines = sndk_pre(cfg, stamp)
        flag = "sndk_pre_notified"
        pri = 4
    else:
        title, lines = sndk_post(cfg, stamp, ir_ok, headline)
        flag = "sndk_post_notified"
        pri = 4

    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("dry-run; no ntfy")
        return 0

    emit(cfg, title, lines, pri)
    state[flag] = True
    state["last_phase"] = phase
    if phase == "sndk_post" and not ir_ok:
        state["fail_notified"] = True
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
