#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "druck.json"
STATE_PATH = ROOT / "state" / "druck.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

PERSON = ("드러켄밀러", "드러켄 밀러", "druckenmiller", "stanley druck")
OFFICIAL = ("공식 확인", "공식확인", "확인했다", "확인했다고", "IR 확인", "공시")
DENY = ("사실이 아니다", "방문하지 않", "회동하지 않", "예정에 없다", "부인")
PHOTO = ("현장 사진", "회동 사진", "포착", "사진으로", "카메라에")
HAN_REPRINT = ("hankyung", "kedglobal", "bloomingbit", "한국경제")
FOREIGN_OK = ("reuters.com", "bloomberg.com", "wsj.com", "ft.com", "cnbc.com", "apnews.com")
QUERIES = [
    "드러켄밀러 (두산 OR 삼성전자 OR SK하이닉스 OR 네이버 OR 쿠팡)",
    "Druckenmiller (Samsung OR Hynix OR Doosan OR Naver OR Coupang)",
]


def now_kst() -> datetime:
    return datetime.now(KST)


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


def in_window(cfg: dict, now: datetime) -> bool:
    if now.year != int(cfg.get("year_guard") or now.year):
        return False
    expire = cfg.get("expire_date")
    if expire and now.date().isoformat() >= expire:
        return False
    days = set()
    for item in cfg.get("oneshot_kst") or []:
        days.add(str(item)[:10])
    return now.strftime("%Y-%m-%d") in days


def http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def rss_items(query: str, hl: str, gl: str, ceid: str) -> list[dict]:
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
    raw = http_bytes(url)
    root = ET.fromstring(raw)
    rows = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        rows.append({"title": title, "link": link, "source": source, "pub": pub})
    return rows


def text_of(row: dict) -> str:
    return f"{row.get('title','')} {row.get('source','')} {row.get('link','')}".lower()


def mentions_person(row: dict) -> bool:
    blob = text_of(row)
    title = row.get("title") or ""
    return any(p in blob or p in title for p in PERSON) or ("드러켄" in title)


def company_hits(cfg: dict, row: dict) -> list[str]:
    title = row.get("title") or ""
    blob = text_of(row)
    hits = []
    for co in cfg.get("companies") or []:
        names = co.get("names") or []
        if any(n.lower() in blob or n in title for n in names):
            hits.append(co["id"])
    return hits


def is_reprint(row: dict) -> bool:
    blob = text_of(row)
    return any(x in blob for x in HAN_REPRINT)


def is_foreign(row: dict) -> bool:
    blob = text_of(row)
    return any(x in blob for x in FOREIGN_OK) and not is_reprint(row)


def classify(cfg: dict, rows: list[dict]) -> dict:
    official = []
    foreign = []
    photos = False
    denied = []
    extra = 0
    for row in rows:
        if not mentions_person(row):
            continue
        cos = company_hits(cfg, row)
        title = row.get("title") or ""
        if any(k in title for k in DENY):
            denied.append(title[:80])
        if any(k in title for k in PHOTO):
            photos = True
        if any(k in title for k in OFFICIAL) and cos:
            official.append({"co": ",".join(cos), "title": title[:80]})
        if is_foreign(row) and cos:
            foreign.append(title[:80])
        if cos and not is_reprint(row):
            extra += 1
    if denied:
        grade = "오류"
    elif official:
        grade = "사실"
    elif foreign:
        grade = "일부 사실"
    elif extra:
        grade = "일부 사실"
    else:
        grade = "확인 불가"
    return {
        "grade": grade,
        "official": official,
        "foreign": foreign[:5],
        "photos": photos,
        "denied": denied[:3],
        "extra": extra,
    }


def alert_key(info: dict) -> str:
    official_ids = sorted({x.get("co") for x in info.get("official") or []})
    return "|".join(
        [
            info.get("grade") or "",
            "photo" if info.get("photos") else "nophoto",
            ",".join(official_ids),
            "fx" if info.get("foreign") else "nofx",
        ]
    )


def body(cfg: dict, now: datetime, info: dict, fetch_fail: int) -> tuple[str, str]:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    grade = info["grade"]
    official = info.get("official") or []
    foreign = info.get("foreign") or []
    photos = info.get("photos")
    if grade == "사실":
        title = "[DRUCK] 회동 공식확인 | 추격매수 금지"
        do_now = "확인된 회동만 사실로 보고, 삼성전자·SK하이닉스 비중은 그대로 두세요. 방한 뉴스만으로 사지 마세요."
    elif grade == "오류":
        title = "[DRUCK] 회동 부인·오류 | 뉴스매수 금지"
        do_now = "한경 단독의 회동 주장은 사실 등급이 내려갔습니다. 관련 추격은 멈추세요."
    elif grade == "일부 사실":
        title = "[DRUCK] 교차보도만 | 매수 보류"
        do_now = "공식 확인은 없습니다. 교차보도만으로 비중을 늘리지 마세요."
    else:
        title = "[DRUCK] 아직 확인 불가 | 비중 유지"
        do_now = "기업 공식 확인이 없습니다. 삼성전자·SK하이닉스 기존 비중을 유지하세요."
    off_txt = ", ".join(f"{x['co']}:{x['title']}" for x in official[:3]) or "없음"
    fx_txt = " / ".join(foreign[:2]) if foreign else "없음"
    fail_txt = f"조회실패 {fetch_fail}회" if fetch_fail else "조회 정상"
    photo_txt = "있음" if photos else "없음"
    lines = [
        f"{stamp} | DRUCK | 드러켄밀러 방한 사실등급",
        "이게 뭐 알림: 9월 22일 전후 두산·삼성·하이닉스·네이버·쿠팡 회동 보도의 사실 등급을 갱신합니다.",
        f"지금 상태: 등급 {grade} | 공식확인 {off_txt} | 현장사진 {photo_txt} | 해외교차 {fx_txt} | {fail_txt}.",
        f"지금 할 일: {do_now}",
        "나중에: 22~23일 창이 끝나면 이 알림은 재실행하지 않습니다. 공시나 기업 확인이 나오면 그때만 다시 보세요.",
        "익절/줄일 때: 이 뉴스로 익절·손절 기준을 바꾸지 마세요. 기존 반도체 규칙만 따르세요.",
        "하지 말 것: 단독 전언 추격매수. 신용·미수. 확인 안 된 회동을 확정으로 단정.",
        f"원문: {cfg.get('source_url')}",
    ]
    return title, "\n".join(lines)


def main() -> int:
    cfg = load_json(
        CFG_PATH,
        {"year_guard": 2026, "expire_date": "2026-09-24", "oneshot_kst": [], "companies": []},
    )
    now = now_kst()
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "window_hit": False,
            "grade": "확인 불가",
            "official": [],
            "photos": False,
            "foreign": [],
            "last_alert_key": None,
            "last_alert_date": None,
            "done": False,
            "fetch_fail": 0,
        },
    )
    if state.get("done") and now.date().isoformat() >= (cfg.get("expire_date") or "2099-01-01"):
        print("expired and done; skip")
        return 0
    if not in_window(cfg, now):
        print(f"outside window {now.isoformat()}; silent")
        return 0

    rows: list[dict] = []
    fetch_fail = 0
    try:
        rows.extend(rss_items(QUERIES[0], "ko", "KR", "KR:ko"))
    except Exception as exc:
        fetch_fail += 1
        print(f"kr rss fail: {exc}", file=sys.stderr)
    try:
        rows.extend(rss_items(QUERIES[1], "en", "US", "US:en"))
    except Exception as exc:
        fetch_fail += 1
        print(f"en rss fail: {exc}", file=sys.stderr)

    if fetch_fail >= 2:
        topic = resolve_topic("ops")
        publish(
            topic,
            "[DRUCK] 조회 실패 | 등급 미갱신",
            "\n".join(
                [
                    f"{now.strftime('%Y-%m-%d %H:%M KST')} | DRUCK | 조회 실패",
                    "이게 뭐 알림: 드러켄밀러 방한 사실등급 점검이 실패했습니다.",
                    "지금 상태: 한글·영문 뉴스 조회가 모두 실패했습니다. 등급은 갱신하지 않았습니다.",
                    "지금 할 일: 실패한 조회로 매매하지 마세요. 한경 원문만 보고 사지 마세요.",
                    "나중에: 다음 기존 슬롯(15:20 또는 다음날 09:00)에서 다시 점검합니다.",
                    "익절/줄일 때: 이번 실패로 비중을 바꾸지 마세요.",
                    "하지 말 것: 실패를 미확인으로 단정하거나, 임의로 재실행을 반복하기.",
                ]
            ),
            4,
            ["warning"],
            resolve_server(cfg.get("ntfy_server")),
        )
        state["fetch_fail"] = int(state.get("fetch_fail") or 0) + 1
        state["updated_at"] = now.isoformat()
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    info = classify(cfg, rows)
    key = alert_key(info)
    today = now.date().isoformat()
    last_day = now.date() == datetime.strptime(cfg["expire_date"], "%Y-%m-%d").date() - timedelta(days=1)
    send = key != state.get("last_alert_key") or (
        last_day and now.hour >= 15 and state.get("last_alert_date") != today
    )
    title, msg = body(cfg, now, info, fetch_fail)
    print(msg)
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    if send:
        publish(
            topic,
            title,
            msg,
            4 if info["grade"] in {"사실", "오류"} else 3,
            ["newspaper"],
            resolve_server(cfg.get("ntfy_server")),
        )
        state["last_alert_key"] = key
        state["last_alert_date"] = today
    else:
        print("no ntfy; duplicate suppressed")

    if today == "2026-09-23" and now.hour >= 15:
        state["done"] = True
    state.update(
        {
            "updated_at": now.isoformat(),
            "window_hit": True,
            "grade": info["grade"],
            "official": info.get("official") or [],
            "photos": bool(info.get("photos")),
            "foreign": info.get("foreign") or [],
            "fetch_fail": fetch_fail,
        }
    )
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
