#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "pce26.json"
STATE_PATH = ROOT / "state" / "pce26.json"
ARCHIVE_PATH = ROOT / "state" / "archive.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

ITEM_RE = {
    "portfolio": re.compile(r"portfolio management|investment advice|asset management|자산운용|포트폴리오", re.I),
    "software": re.compile(r"computer software|software and accessories|소프트웨어", re.I),
    "legal": re.compile(r"legal services|법률서비스|법률 서비스", re.I),
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text))


def parse_pct(text: str, patterns: list[str]):
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def parse_bea(text: str) -> dict:
    yoy_head = parse_pct(
        text,
        [
            r"PCE price index increased ([0-9.]+) percent from one year ago",
            r"PCE price index (?:increased|rose) ([0-9.]+) percent .*(?:year|year ago)",
            r"increased ([0-9.]+) percent from the same month one year ago",
        ],
    )
    yoy_core = parse_pct(
        text,
        [
            r"Excluding food and energy, the PCE price index increased ([0-9.]+) percent from one year ago",
            r"excluding food and energy[\s\S]{0,80}increased ([0-9.]+) percent from one year ago",
        ],
    )
    mom_head = parse_pct(
        text,
        [
            r"PCE price index for August (?:increased|decreased|was unchanged at) ([0-9.]+) percent",
            r"From the preceding month, the PCE price index for August (?:increased|decreased) ([0-9.]+) percent",
        ],
    )
    mom_core = parse_pct(
        text,
        [
            r"Excluding food and energy, the PCE price index (?:increased|decreased) ([0-9.]+) percent",
        ],
    )
    if re.search(r"PCE price index for August decreased", text, re.I) and mom_head is not None:
        mom_head = -abs(mom_head)
    published = bool(
        re.search(r"Personal Income and Outlays, August 2026", text, re.I)
        and (yoy_core is not None or yoy_head is not None)
        and not re.search(r"EMBARGOED UNTIL RELEASE AT 8:30 a\.m\. EDT, Wednesday, September 30", text)
    )
    items = {k: bool(rx.search(text)) for k, rx in ITEM_RE.items()}
    return {
        "published": published,
        "yoy_head": yoy_head,
        "yoy_core": yoy_core,
        "mom_head": mom_head,
        "mom_core": mom_core,
        "items": items,
    }


def last_fred(url: str, min_date: str):
    raw = fetch(url)
    rows = []
    for line in raw.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        date, val = parts[0].strip(), parts[1].strip()
        if val in {".", ""}:
            continue
        try:
            rows.append((date, float(val)))
        except ValueError:
            continue
    rows = [r for r in rows if r[0] >= min_date]
    return rows[-1] if rows else None


def load_state() -> dict:
    state = {
        "final_notified": False,
        "fail_notified": False,
        "empty_notified": False,
        "audit_notified": False,
        "last_key": None,
        "updated_at": None,
        "bea_ok": None,
        "fred_ok": None,
        "trim_ok": None,
        "parsed": {},
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fmt(v, suffix="%"):
    return f"{v:.2f}{suffix}" if isinstance(v, (int, float)) else "확인 불가"


def delta(new, old):
    if new is None or old is None:
        return "확인 불가"
    d = new - old
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.2f}%p"


def fomc_line(core_yoy, trim_yoy, core_delta) -> str:
    if core_yoy is None:
        return "숫자 미확인. 10월 FOMC 함의를 단정하지 않음."
    cool = core_yoy <= 3.05 and (trim_yoy is None or trim_yoy <= 2.20)
    still_hot = core_yoy >= 3.20
    if still_hot:
        return "근원 3.2% 이상이면 9/16 인상 후 10/28 인하 근거는 약함. 동결 또는 추가인상 쪽."
    if cool:
        return "근원과 절사평균이 함께 낮아도 9/16 만장일치 인상 직후라 10/28 인하로 바로 읽지 말 것. 성명 톤만 후속."
    extra = f" 개정폭 {core_delta}." if core_delta != "확인 불가" else ""
    return "목표 2%와 갭이 남음. 10/28은 SEP 없는 회의. 인하 단정 금지." + extra


def emit(cfg: dict, title: str, lines: list[str], priority: int) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["chart_with_upwards_trend"], server)


def archive_hits() -> tuple[bool, list[str]]:
    if not ARCHIVE_PATH.exists():
        return False, []
    try:
        payload = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, []
    titles = []
    for item in payload.get("items") or []:
        title = str(item.get("title") or "")
        if "[PCE26]" in title:
            titles.append(title)
    return True, titles[:6]


def maybe_audit(cfg: dict, state: dict, now: datetime, mode: str) -> bool:
    if state.get("audit_notified") and mode != "force":
        return False
    parsed = state.get("parsed") or {}
    core_yoy = parsed.get("core_yoy")
    trim_yoy = parsed.get("trim_yoy")
    trim_date = parsed.get("trim_date")
    arch_ok, titles = archive_hits()
    sent = bool(state.get("final_notified"))
    failed = bool(state.get("fail_notified"))
    empty = bool(state.get("empty_notified"))
    in_archive = any("[PCE26]" in t for t in titles)
    if sent:
        status = "본문 발송됨"
    elif failed:
        status = "조회 실패 발송됨"
    elif empty:
        status = "발표문 없음 발송됨"
    else:
        status = "state상 미발송"
    if arch_ok and not in_archive and sent:
        status += ". 아카브에는 제목 없음"
    elif arch_ok and in_archive:
        status += ". 아카브 확인"
    elif not arch_ok:
        status += ". 아카브 파일 없음"
    title = f"[PCE26] 점검 | {status}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | PCE26 | 발송 점검",
        "이게 뭐 알림: 10/1 09:00 이후 state와 ntfy 아카브만 읽어 발송 여부를 확인합니다.",
        (
            f"지금 상태: {status}. 근원 {fmt(core_yoy)} / 절사평균 {fmt(trim_yoy)}"
            f"({trim_date or '날짜없음'}). "
            f"final={state.get('final_notified')} fail={state.get('fail_notified')} "
            f"empty={state.get('empty_notified')} bea={state.get('bea_ok')} trim={state.get('trim_ok')}."
        ),
        "아카브 제목: " + (" | ".join(titles) if titles else "PCE26 없음"),
        "지금 할 일: 미발송이면 숫자를 추정하지 말고 다음 슬롯 또는 원문을 보세요.",
        "나중에: 이 점검은 한 번만 보냅니다. 만료 2026-10-02.",
        "익절/줄일 때: 점검 알림만으로 포지션 바꾸지 마세요.",
        "하지 말 것: 아카브 없음을 물가 둔화로 읽기.",
    ]
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("dry-run audit; no ntfy")
        return False
    emit(cfg, title, lines, 3)
    state["audit_notified"] = True
    return True


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    state = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    audit_after = datetime.fromisoformat(cfg.get("audit_after_kst", "2026-10-01 15:00")).replace(tzinfo=KST)
    in_window = start <= now <= expire and now.year == int(cfg.get("event_year", 2026))
    want_audit = now >= audit_after and now <= expire and now.year == int(cfg.get("event_year", 2026)) and not state.get("audit_notified")
    print(f"{now.isoformat()} mode={mode} window={in_window} audit={want_audit}")

    if mode not in {"force", "dry"} and not in_window and not want_audit:
        print("outside 2026 window; silent")
        save_state(state, now)
        return 0

    if want_audit and (state.get("final_notified") or state.get("fail_notified") or state.get("empty_notified") or mode in {"force", "dry"}):
        maybe_audit(cfg, state, now, mode)
        save_state(state, now)
        if state.get("final_notified") and mode != "force":
            print("audit done after final; skip refetch")
            return 0

    if state.get("final_notified") and mode != "force":
        print("already sent final; skip")
        save_state(state, now)
        return 0

    pre = cfg["pre_revision"]
    bea = {}
    bea_ok = False
    bea_err = ""
    try:
        html = fetch(cfg["sources"]["bea"])
        bea = parse_bea(strip_html(html))
        bea_ok = True
    except Exception as exc:
        bea_err = str(exc)
        print(f"bea fail: {exc}")

    fred_core = fred_head = fred_trim = None
    fred_ok = trim_ok = False
    try:
        fred_core = last_fred(cfg["sources"]["fred_core"], "2026-07-01")
        fred_head = last_fred(cfg["sources"]["fred_head"], "2026-07-01")
        fred_ok = bool(fred_core or fred_head)
    except Exception as exc:
        print(f"fred fail: {exc}")
    try:
        fred_trim = last_fred(cfg["sources"]["fred_trim"], "2026-07-01")
        trim_ok = bool(fred_trim)
    except Exception as exc:
        print(f"trim fail: {exc}")

    state["bea_ok"] = bea_ok
    state["fred_ok"] = fred_ok
    state["trim_ok"] = trim_ok

    core_yoy = bea.get("yoy_core")
    head_yoy = bea.get("yoy_head")
    if core_yoy is None and fred_core and fred_core[0] >= "2026-08-01":
        core_yoy = fred_core[1]
    if head_yoy is None and fred_head and fred_head[0] >= "2026-08-01":
        head_yoy = fred_head[1]
    trim_yoy = fred_trim[1] if fred_trim and fred_trim[0] >= "2026-07-01" else None
    trim_date = fred_trim[0] if fred_trim else None
    published = bool(bea.get("published")) or (core_yoy is not None and (fred_core and fred_core[0] >= "2026-08-01"))

    parsed = {
        "core_yoy": core_yoy,
        "head_yoy": head_yoy,
        "mom_core": bea.get("mom_core"),
        "mom_head": bea.get("mom_head"),
        "trim_yoy": trim_yoy,
        "trim_date": trim_date,
        "items": bea.get("items") or {},
        "published": published,
    }
    state["parsed"] = parsed

    if mode != "dry" and not bea_ok and not fred_ok:
        if not state.get("fail_notified"):
            emit(
                cfg,
                "[PCE26] 조회 실패 | 재시도",
                [
                    f"{now.strftime('%Y-%m-%d %H:%M KST')} | PCE26 | 8월 PCE 연례개정",
                    f"이게 뭐 알림: {cfg['explain']}",
                    f"지금 상태: BEA, FRED 조회 실패. {bea_err or '출처 열리지 않음'}. 물가가 내려간 것으로 처리하지 않음.",
                    "지금 할 일: 다음 기존 슬롯까지 대기. 지금은 포지션 바꾸지 마세요.",
                    "나중에: 원문이 열리면 한 번만 비교표를 보냅니다. 만료 2026-10-02.",
                    "익절/줄일 때: 이번 실패 알림으로는 매매하지 마세요.",
                    "하지 말 것: 조회 실패를 물가 둔화로 읽기.",
                ],
                4,
            )
            state["fail_notified"] = True
        if want_audit:
            maybe_audit(cfg, state, now, mode)
        save_state(state, now)
        return 0

    last_day = now.date() == expire.date()
    send = False
    kind = "wait"
    if published:
        send = True
        kind = "final"
    elif last_day or mode in {"force", "dry"}:
        send = True
        kind = "empty" if not published else "final"

    core_delta = delta(core_yoy, pre["july_core_yoy"])
    items = parsed["items"]
    item_txt = " / ".join(
        f"{label} {'본문언급' if items.get(key) else '본문없음'}"
        for key, label in (("portfolio", "자산운용"), ("software", "소프트웨어"), ("legal", "법률"))
    )
    if not bea_ok:
        item_txt = "BEA 본문 실패. 항목별 기여도는 확인 불가"
    elif not any(items.values()):
        item_txt = "3항목 기여도 수치는 확인 불가. 본문에서 항목명도 못 찾음"

    title = "[PCE26] 아직 결과 없음 | 대기"
    if kind == "final" and core_yoy is not None:
        if core_yoy >= 3.20:
            title = f"[PCE26] 근원 {fmt(core_yoy)} | 10월 인하 보류"
        elif core_yoy <= 3.05:
            title = f"[PCE26] 근원 {fmt(core_yoy)} | 인하 단정 금지"
        else:
            title = f"[PCE26] 근원 {fmt(core_yoy)} | 동결 쪽 유지"
    elif kind == "empty":
        title = "[PCE26] 발표문 없음 | 대기"

    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | PCE26 | 8월 PCE + 연례개정",
        f"이게 뭐 알림: {cfg['explain']}",
        (
            "지금 상태: "
            + (
                f"헤드 {fmt(head_yoy)} / 근원 {fmt(core_yoy)} m/m {fmt(parsed['mom_core'])} / "
                f"절사평균 {fmt(trim_yoy)}({trim_date or '날짜없음'}). "
                f"7월 근원 {pre['july_core_yoy']}% 대비 {core_delta}. "
                f"7월 절사 {pre['july_trim_yoy']}% 대비 {delta(trim_yoy, pre['july_trim_yoy'])}."
                if kind == "final"
                else "8월 수치가 아직 안 열림. 물가 둔화로 처리하지 않음."
            )
        ),
        f"항목: {item_txt}",
        f"지금 할 일: {fomc_line(core_yoy, trim_yoy, core_delta) if kind == 'final' else '발표 확인 전 포지션 유지.'}",
        "나중에: 10/28 FOMC는 SEP 없음. 9월 PCE는 10/29라 회의 후에 나옴. 같은 결과는 다시 안 보냄.",
        "익절/줄일 때: 근원이 0.2%p 내려도 추격 매수/인하 베팅 금지. 9/16 인상과 SEP 3.4%를 먼저 보세요.",
        "하지 말 것: 방법론 개정을 실물 물가 급락으로 읽기. 조회 실패를 둔화로 처리.",
        f"출처: {cfg['sources']['bea']}",
        f"조회: BEA {'OK' if bea_ok else '실패'} / FRED {'OK' if fred_ok else '실패'} / 절사평균 {'OK' if trim_ok else '실패'}",
    ]

    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("dry-run; no ntfy")
        save_state(state, now)
        return 0

    if not send:
        print("condition not met; silent")
        if want_audit:
            maybe_audit(cfg, state, now, mode)
        save_state(state, now)
        return 0
    key = json.dumps(parsed, sort_keys=True)
    if state.get("last_key") == key and state.get("final_notified"):
        print("same key; silent")
        if want_audit:
            maybe_audit(cfg, state, now, mode)
        save_state(state, now)
        return 0

    emit(cfg, title, lines, 4 if kind == "final" else 3)
    if kind == "final":
        state["final_notified"] = True
    else:
        state["empty_notified"] = True
    state["last_key"] = key
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
