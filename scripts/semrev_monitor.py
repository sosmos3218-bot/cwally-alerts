#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "semrev.json"
STATE_PATH = ROOT / "state" / "semrev.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

AMD_POS = ("공식", "확정", "방문했다", "방한했다", "만났다", "체결", "공급 계약", "우선 공급", "ir ", "press")
AMD_NEG = ("추진", "검토", "소식통", "알려졌다", "전망", "예상", "sources", "expected to visit")
AMD_NEED = ("리사 수", "lisa su", "amd")
AMD_KO = ("방한", "한국", "서울", "삼성")
ETF_NEED = ("리밸런싱", "정기변경", "수시변경")
ETF_NAME = ("tiger 반도체top10", "반도체top10", "sol ai반도체", "396500", "ai반도체top2")
ETF_SIZE = ("비중", "편입", "편출", "억원", "조원", "구성")
OFFICIAL_HOST = ("amd.com", "samsung.com", "yonhapnews", "yna.co.kr")


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = dict(fallback)
        out.update(data)
        return out
    except json.JSONDecodeError:
        return dict(fallback)


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def parse_uk_shares(text: str) -> int:
    cleaned = text.replace(",", "").replace("+", "").strip()
    if not cleaned or cleaned == "-":
        return 0
    return int(float(cleaned))


def fetch_foreign_days(code: str, need: int) -> list[dict]:
    url = f"https://finance.naver.com/item/frgn.naver?code={code}"
    html = fetch(url)
    rows = []
    for block in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", block, flags=re.S | re.I)
        texts = [re.sub(r"<[^>]+>", "", c) for c in cells]
        texts = [re.sub(r"\s+", "", t) for t in texts]
        if len(texts) < 7:
            continue
        m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", texts[0])
        if not m:
            continue
        day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        try:
            foreign = parse_uk_shares(texts[6] if len(texts) > 6 else texts[-3])
        except ValueError:
            continue
        rows.append({"day": day, "foreign": foreign})
        if len(rows) >= need + 2:
            break
    rows.sort(key=lambda x: x["day"], reverse=True)
    return rows[:need]


def flow3_hit(cfg: dict) -> tuple[bool, str, dict]:
    need = int(cfg.get("flow", {}).get("consecutive_days") or 3)
    tickers = cfg["tickers"]
    bag = {}
    notes = []
    ok = True
    for key, spec in tickers.items():
        rows = fetch_foreign_days(spec["code"], need)
        bag[key] = rows
        if len(rows) < need:
            ok = False
            notes.append(f"{spec['display']} 수급 {len(rows)}일만 조회")
            continue
        days = rows[:need]
        pos = all(r["foreign"] > 0 for r in days)
        bits = ", ".join(f"{r['day'][5:]} {r['foreign']:+,}" for r in days)
        notes.append(f"{spec['display']} 외인 {bits}")
        if not pos:
            ok = False
    return ok, " / ".join(notes), bag


def rss_items(url: str) -> list[dict]:
    try:
        xml = fetch(url, timeout=20)
        root = ET.fromstring(xml)
    except Exception as exc:
        print(f"rss fail {url}: {exc}", file=sys.stderr)
        return []
    items = []
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", node.findtext("description") or "")
        items.append({"title": title, "link": link, "text": f"{title} {desc} {link}".lower()})
    return items


def amd_hit(items: list[dict]) -> tuple[bool, str]:
    picked = []
    for it in items:
        t = it["text"]
        if not any(k in t for k in AMD_NEED):
            continue
        if not any(k in t for k in AMD_KO) and "korea" not in t:
            continue
        if any(k in t for k in AMD_NEG) and not any(k in t for k in AMD_POS):
            continue
        host_ok = any(h in t for h in OFFICIAL_HOST)
        pos_ok = any(k in t for k in AMD_POS)
        if host_ok or pos_ok:
            picked.append(it["title"][:80])
    if not picked:
        return False, "공식 방한·후속계약 미확인"
    return True, " / ".join(picked[:3])


def etf_hit(items: list[dict]) -> tuple[bool, str]:
    picked = []
    for it in items:
        t = it["text"]
        if not any(k in t for k in ETF_NEED):
            continue
        if not any(k in t for k in ETF_NAME):
            continue
        if "10월" not in t and "10/" not in t and "oct" not in t:
            continue
        if not any(k in t for k in ETF_SIZE):
            continue
        picked.append(it["title"][:80])
    if not picked:
        return False, "10월 ETF 리밸런싱 공지(규모·종목) 미확인"
    return True, " / ".join(picked[:3])


def main() -> int:
    dry = "dry" in [a.lower() for a in sys.argv[1:]]
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(
        STATE_PATH,
        {"updated_at": None, "fired": {"amd": False, "flow3": False, "etf": False}, "last_flow": {}, "last_sent": None, "last_hits": []},
    )
    fired = dict(state.get("fired") or {})
    for key in ("amd", "flow3", "etf"):
        fired.setdefault(key, False)

    if now.year != int(cfg.get("year_guard") or 2026):
        print("semrev year-guard silent")
        return 0
    expire = datetime.strptime(cfg["expire_date"], "%Y-%m-%d").date()
    if now.date() > expire:
        print("semrev expired silent")
        return 0
    if now.weekday() >= 5:
        print("semrev weekend silent")
        return 0

    items: list[dict] = []
    for url in cfg.get("sources", {}).get("rss") or []:
        items.extend(rss_items(url))

    amd_ok, amd_note = amd_hit(items)
    etf_ok, etf_note = etf_hit(items)
    try:
        flow_ok, flow_note, flow_bag = flow3_hit(cfg)
    except Exception as exc:
        print(f"flow fetch fail: {exc}", file=sys.stderr)
        flow_ok, flow_note, flow_bag = False, f"수급 조회 실패: {exc}", {}
        title = "[SEMREV] 수급 조회 실패 | 판정 보류"
        body = "\n".join(
            [
                f"{stamp} | SEMREV | 사용자 Cwally",
                "이게 뭐 알림: 반도체 재검토 조건 점검 중 수급 조회가 실패했습니다.",
                f"지금 상태: {flow_note}",
                "지금 할 일: 실패를 정상으로 보지 마세요. 네이버 외인 수급을 직접 확인하세요.",
                "나중에: 다음 09:00/15:20 슬롯에서 다시 봅니다.",
                "익절/줄일 때: 이 실패로 사지 마세요.",
                "하지 말 것: 조회 실패를 매수 신호로 해석. 신용/미수.",
            ]
        )
        print(body)
        if not dry and state.get("last_fail_day") != now.strftime("%Y-%m-%d"):
            publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
            state["last_fail_day"] = now.strftime("%Y-%m-%d")
            state["updated_at"] = stamp
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    new_hits = []
    if amd_ok and not fired.get("amd"):
        fired["amd"] = True
        new_hits.append("amd")
    if flow_ok and not fired.get("flow3"):
        fired["flow3"] = True
        new_hits.append("flow3")
    if etf_ok and not fired.get("etf"):
        fired["etf"] = True
        new_hits.append("etf")

    state["updated_at"] = stamp
    state["fired"] = fired
    state["last_flow"] = flow_bag
    state["last_notes"] = {"amd": amd_note, "flow3": flow_note, "etf": etf_note}
    print(stamp, "amd", amd_ok, "flow3", flow_ok, "etf", etf_ok, "new", new_hits)

    if not new_hits:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("semrev silent; no new change-condition")
        return 0

    labels = {
        "amd": "AMD 공식 방한/후속계약",
        "flow3": "외인 3일 연속 순매수",
        "etf": "10월 ETF 리밸런싱 공지",
    }
    title = "[SEMREV] " + " / ".join(labels[k] for k in new_hits) + " | 영상 재검토"
    body = "\n".join(
        [
            f"{stamp} | SEMREV | 사용자 Cwally",
            "이게 뭐 알림: 반도체 ETF 영상 판단을 다시 볼 조건입니다. 매수 신호가 아닙니다.",
            f"지금 상태: AMD {amd_note}. 수급 {flow_note}. ETF {etf_note}.",
            "지금 할 일: 해당 조건의 원문(공식 보도/운용 공지/외인 3일 숫자)만 확인하고, 영상 서술을 다시 보세요.",
            "나중에: 10월 31일까지 같은 슬롯에서 나머지 조건만 봅니다. 이미 보낸 조건은 다시 안 보냅니다.",
            "익절/줄일 때: 이 알림으로 사지 마세요. 기존 [SEC] 구간 규칙을 따르세요.",
            "하지 말 것: 소식통/추진 기사를 공식으로 취급. 추격. 신용/미수.",
        ]
    )
    print(body)
    if not dry:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["mag"], resolve_server(cfg.get("ntfy_server")))
        state["last_sent"] = {"at": stamp, "hits": new_hits}
        state["last_hits"] = new_hits
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
