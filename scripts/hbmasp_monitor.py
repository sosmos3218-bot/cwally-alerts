#!/usr/bin/env python3
"""HBM blended-ASP baseline reminder, plus a human-review outlook pass. Standard library only. Not a trade signal.

Active window for the morning baseline/notice path: configs/hbmasp.json active_from..expire_date and year_guard
(an int or a list of years). ntfy_util.publish also stops at ends_kst for that path.
Outlook (argv outlook) is separate: weekday 19:00 k203780 slot only, its own ends_kst under config "outlook".
It watches three public lists and asks a person to review new HBM outlook / LTA / capacity material.
It does not turn a new article into a confirmed forecast revision or a trade.
Modes: (none) run | outlook [auto|dry] | --install-confirm | --selftest or selftest
(offline fixtures: stubbed ntfy and notice feed, temp state, no network).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "hbmasp.json"
STATE_PATH = ROOT / "state" / "hbmasp.json"
KST = timezone(timedelta(hours=9))
NOTICE_RE = re.compile(r"HBM|고대역폭|공급가|판매가|공급계약|단일판매", re.I)


def helper(name: str):
    try:
        return __import__(name)
    except ImportError:
        flat = ROOT.parent / "ntfy-source" / f"scripts__{name}.py"
        if not flat.exists():
            raise
        spec = importlib.util.spec_from_file_location(name, flat)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return copy.deepcopy(fallback)
    data = json.loads(path.read_text(encoding="utf-8"))
    out = copy.deepcopy(fallback)
    if isinstance(data, dict):
        out.update(data)
    return out


def atomic_save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def default_state() -> dict:
    return {
        "schema_version": 1,
        "last_baseline_week": None,
        "notice_versions": [],
        "ops_daily": {},
        "updated_at": None,
    }


def body(stamp: str, what: str, status: str, action: str, later: str, reduce: str, avoid: str, extra: list[str] | None = None) -> str:
    lines = [
        f"{stamp} | HBMASP",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {reduce}",
        f"하지 말 것: {avoid}",
    ]
    return "\n".join(lines + (extra or []))


def baseline_extra(cfg: dict) -> list[str]:
    b = cfg["baseline"]
    return [
        f"기준: {b['lock']}",
        b["offer_not_contract"],
        b["later_report"],
        b["trendforce"],
        b["kb"],
        b["sk"],
        b["goldman"],
    ] + [b[k] for k in ("dt_1026", "eugene") if b.get(k)] + [b["direction"]]


def year_ok(cfg: dict, year: int) -> bool:
    yg = cfg["year_guard"]
    return year in [int(y) for y in yg] if isinstance(yg, list) else year == int(yg)


def notice_key(code: str, item: dict) -> str:
    raw = "|".join((code, str(item.get("id") or ""), str(item.get("title") or ""), str(item.get("datetime") or "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def publish(ntfy, server: str, title: str, message: str, priority: int = 3) -> None:
    print(f"channel=cwally-port title={title}")
    print(message)
    ntfy.publish("cwally-port", title, message, priority, ["chart_with_upwards_trend"], server)


def install_confirm(now: datetime) -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ntfy = helper("ntfy_util")
    server = ntfy.resolve_server(cfg.get("ntfy_server"))
    stamp = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    message = body(
        stamp,
        "HBM 블렌디드 ASP 기준 알림 설치 확인입니다. 매매 신호가 아닙니다.",
        "비교 기준은 블렌디드 ASP. 제시가 3배는 확정가가 아님. 금액 민감도는 없음.",
        "확정 계약가와 회사 가이던스만 보고, 제시가를 영업이익에 넣지 마세요.",
        "같은 기관이 ASP 두 시나리오 영업이익을 공개하면 그때만 금액 비교.",
        "이 알림으로 줄이지 마세요. 최종가가 2배 안팎이면 3배 프레임을 폐기.",
        "연도가 다른 점추정을 차액으로 읽기. 4달러대 중후반을 확정 ASP로 읽기.",
        baseline_extra(cfg),
    )
    publish(ntfy, server, "[HBMASP] 005930/000660 설치 확인 | 매매 신호 아님", message, 3)
    print("install-confirm sent; state not written")
    return 0


def run(now: datetime | None = None) -> int:
    now = (now or datetime.now(KST)).astimezone(KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, default_state())
    work = copy.deepcopy(state)
    stamp, today = now.strftime("%Y-%m-%d %H:%M KST"), now.date().isoformat()
    if not year_ok(cfg, now.year) or not (cfg["active_from"] <= today <= cfg["expire_date"]):
        print(f"{stamp} outside active window; silent")
        return 0
    ntfy = helper("ntfy_util")
    server = ntfy.resolve_server(cfg.get("ntfy_server"))
    changed = False
    week = now.strftime("%G-W%V")
    if now.weekday() == 0 and state.get("last_baseline_week") != week:
        message = body(
            stamp,
            "HBM 가격 기준 주간 점검입니다. 매수 신호가 아닙니다.",
            "비교 기준은 블렌디드 ASP. 제시가 3배는 확정가가 아님. 하이닉스 탄력성이 크고 삼성은 연결에서 희석.",
            "확정 계약가와 회사 가이던스만 확인하세요. 제시가를 영업이익에 넣지 마세요.",
            "같은 기관의 ASP 두 시나리오가 나오기 전에는 금액 민감도를 만들지 않습니다.",
            "이 알림으로 줄이지 마세요. 최종가가 2배 안팎이면 3배 프레임을 폐기.",
            "연도가 다른 점추정을 차액으로 읽기. 4달러대 중후반을 확정 ASP로 읽기.",
            baseline_extra(cfg),
        )
        try:
            publish(ntfy, server, "[HBMASP] 005930/000660 기준 유지 | 제시가를 확정가로 보지 말 것", message, 3)
            work["last_baseline_week"] = week
            changed = True
        except Exception as exc:
            print(f"publish failed baseline: {exc}", file=sys.stderr)
            return 1
    naver = helper("naver_api")
    seen = set(state.get("notice_versions") or [])
    errors = []
    for code, name in cfg["notices"]["codes"].items():
        try:
            items = naver.notices(code, size=cfg["notices"]["size"])
            if not isinstance(items, list):
                raise ValueError("공시 목록 형식 오류")
            for item in items:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                if not NOTICE_RE.search(item["title"]):
                    continue
                key = notice_key(code, item)
                if key in seen:
                    continue
                message = body(
                    stamp,
                    f"{name} HBM 가격, 계약 제목 공시 원문 확인 요청입니다. 확정 판정이 아닙니다.",
                    f"제목: {item.get('title')}",
                    "원문에서 제시가인지 확정 계약가인지 직접 나누세요.",
                    "확정가 또는 회사 ASP 가이던스가 나오면 기준을 실적 민감도로 바꿉니다.",
                    "제목만으로 보유를 줄이지 마세요.",
                    "제목을 확정 계약, 3배 ASP, 영업이익 민감도로 읽기.",
                    [f"출처: {item.get('url') or '주소 없음'}"],
                )
                publish(ntfy, server, "[HBMASP] 005930/000660 새 공시 | 제시가와 확정가 구분", message, 4)
                work.setdefault("notice_versions", []).append(key)
                seen.add(key)
                work["notice_versions"] = work["notice_versions"][-500:]
                changed = True
        except Exception as exc:
            errors.append(f"{name} 공시 조회 실패: {exc}")
    if errors and state.get("ops_daily", {}).get(today) != "notice":
        ops = "[HBMASP] 005930/000660 공시 조회 실패 | 매매 신호 아님"
        message = body(
            stamp,
            "HBM 공시 목록 조회 실패 안내입니다. 매매 신호가 아닙니다.",
            " / ".join(errors),
            "다음 평일 09:00에 다시 봅니다. 오늘 기준은 유지하세요.",
            "원문이 없으면 제시가를 확정가로 올리지 않습니다.",
            "조회 실패로 보유를 줄이지 마세요.",
            "빈 결과를 계약 없음 또는 가격 확정으로 읽기.",
        )
        try:
            print(f"channel=cwally-ops title={ops}")
            ntfy.publish("cwally-ops", ops, message, 3, ["warning"], server)
            work.setdefault("ops_daily", {})[today] = "notice"
            changed = True
        except Exception as exc:
            print(f"ops publish failed: {exc}", file=sys.stderr)
    if changed:
        work["updated_at"] = stamp
        atomic_save(STATE_PATH, work)
        print("state updated")
    else:
        print(f"{stamp} no new baseline or notice")
    return 0


# ---------- outlook: public HBM outlook / LTA / capacity, human review only ----------
# Morning baseline numbers stay in config "baseline". This pass stores no price threshold.

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
REVIEW_TITLE = "[HBMASP] 005930/000660 신규 자료 | 변경 여부 검토"
COMPARE_TITLE = "[HBMASP] 005930/000660 수치 비교 | 사람 검토"
HBM_TITLE_RE = re.compile(r"\bHBM\d*[Ee]?\b|high[-\s]?bandwidth memory|고대역폭", re.I)
EARN_TITLE_RE = re.compile(r"financial results|earnings guidance|business results|announces\s+.{0,40}?results|실적발표|잠정실적", re.I)
CAP_TITLE_RE = re.compile(r"production capacity|capacity expansion|production base|증설|생산능력", re.I)
EXCLUDE_TITLE_RE = re.compile(r"invitation|access details|analyst interview|tech note|hands-on|galaxy|smartphone|share repurchase|shareholder return", re.I)
BODY_OK_RE = re.compile(r"\bHBM\d*[Ee]?\b|high[-\s]?bandwidth memory|\bLTAs?\b|long-term agreement|장기공급", re.I)
_HEDGE_RE = re.compile(r"\b(about|around|approximately|approx|nearly|roughly|more than|up to|over\s+\d)\b|약|이상|미만|정도", re.I)
_RANGE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:~|–|—|-|to)\s*\d")
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


class FeedError(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind  # 조회 실패 | 파싱 실패
        self.detail = detail


def _ops_title(label: str, kind: str) -> str:
    return f"[HBMASP] 005930/000660 {label} {kind} | 매매 신호 아님"


def _sanitize(text: str) -> str:
    out = unescape(text or "")
    for ch, repl in (("\u00b7", ", "), ("\u2014", "-"), ("\u2013", "-"), ("\u2212", "-")):
        out = out.replace(ch, repl)
    return re.sub(r"\s+", " ", out).strip()


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url.strip()).hostname or "").lower()
    except Exception:
        return ""


def canon_url(url: str) -> str:
    part = urllib.parse.urlsplit((url or "").strip())
    host = (part.hostname or "").lower()
    path = re.sub(r"/+$", "", part.path or "")
    return host + path


def canon_title(title: str) -> str:
    text = _sanitize(title).lower().replace("'", "'")
    text = re.sub(r"[^a-z0-9가-힣%+]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _blank_source() -> dict:
    return {"seeded": False, "seen": [], "claims": {}, "ops_day": None, "last_ok": None}


def _mark_seen(slot: dict, item: dict) -> None:
    keys = ["u:" + item["key"], "t:" + canon_title(item["title"])]
    seen = slot.setdefault("seen", [])
    for key in keys:
        if key and key not in seen:
            seen.append(key)
    if len(seen) > 800:
        del seen[:-800]


def _seen(slot: dict, item: dict) -> bool:
    seen = set(slot.get("seen") or [])
    return ("u:" + item["key"]) in seen or ("t:" + canon_title(item["title"])) in seen


def _claim_key(claim: dict) -> str:
    return "|".join([claim["metric"], claim["year"], claim["unit"], claim["gen"], claim["mix"], claim["stack"]])


def parse_claim(text: str) -> dict | None:
    """One unambiguous comparable claim, or None. Title and RSS description only."""
    found: list[dict] = []
    parts = re.split(r"(?<=[.!?])\s+", _sanitize(text))
    for sent in parts:
        if not sent or _HEDGE_RE.search(sent) or _RANGE_RE.search(sent):
            continue
        if len(re.findall(r"\d+(?:\.\d+)?\s*%", sent)) > 1:
            continue
        years = re.findall(r"20\d{2}", sent)
        if len(years) != 1:
            continue
        if re.search(r"\bblended\b", sent, re.I) and re.search(r"\bHBM\b", sent) and re.search(r"\bASP\b", sent, re.I) and re.search(r"YoY|year-over-year|year on year", sent, re.I):
            matched = re.search(r"([+-]?\d{1,3}(?:\.\d+)?)\s*%", sent)
            if matched:
                num = float(matched.group(1))
                if -100 <= num <= 500:
                    found.append({"metric": "asp_yoy", "year": years[0], "unit": "pct", "gen": "blended", "mix": "blended", "stack": "", "num": num, "sent": sent[:180]})
        gen = re.search(r"\b(HBM4E|HBM3E|HBM4)\b", sent, re.I)
        price = re.search(r"\$(\d+(?:\.\d+)?)\s*(?:/|per\s+)(Gb|GB)\b", sent)
        has_gb = "Gb" in sent
        has_GB = "GB" in sent
        if gen and price and not (has_gb and has_GB) and len(re.findall(r"\$\d", sent)) == 1:
            num = float(price.group(1))
            if 0 < num < 100:
                stack = re.search(r"(\d{1,2})\s*-\s*high\b", sent, re.I)
                found.append({
                    "metric": "price", "year": years[0], "unit": "usd_per_" + price.group(2),
                    "gen": gen.group(1).upper(), "mix": "same_gen", "stack": stack.group(1) if stack else "",
                    "num": num, "sent": sent[:180],
                })
    uniq: list[dict] = []
    for claim in found:
        if all(_claim_key(claim) != _claim_key(prev) or claim["num"] != prev["num"] for prev in uniq):
            uniq.append(claim)
    if len(uniq) != 1:
        return None
    return uniq[0]


def _unit_label(unit: str) -> str:
    return {"pct": "%", "usd_per_Gb": "USD/Gb", "usd_per_GB": "USD/GB"}.get(unit, unit)


def _mix_label(claim: dict) -> str:
    if claim["mix"] == "blended":
        return "블렌디드"
    stack = f" {claim['stack']}-high" if claim["stack"] else ""
    return f"동일 세대 {claim['gen']}{stack}"


def mentioned_years(text: str) -> list[str]:
    out: list[str] = []
    for year in re.findall(r"20\d{2}", text or ""):
        if year not in out:
            out.append(year)
    return out


def parse_rss(text: str, allowed: set[str]) -> list[dict]:
    if "<rss" not in text.lower() and "<feed" not in text.lower():
        raise FeedError("파싱 실패", "RSS 형식이 아님")
    blocks = re.findall(r"<item\b[^>]*>(.*?)</item>", text, flags=re.S | re.I)
    if not blocks:
        raise FeedError("파싱 실패", "항목이 없음")
    items: list[dict] = []
    seen: set[str] = set()
    for block in blocks:
        def tag(name: str, seg: str = block) -> str:
            matched = re.search(rf"<{name}[^>]*>(.*?)</{name}>", seg, flags=re.S | re.I)
            if not matched:
                return ""
            return re.sub(r"<!\[CDATA\[|\]\]>", "", matched.group(1))

        link_m = re.search(r"<link>(.*?)</link>", block, flags=re.S | re.I)
        link = unescape((link_m.group(1) if link_m else "").strip())
        title = _sanitize(re.sub(r"<[^>]+>", " ", tag("title")))
        desc = _sanitize(re.sub(r"<[^>]+>", " ", tag("description")))
        if _host(link) not in allowed or not title:
            continue
        key = canon_url(link)
        if not key or key in seen:
            continue
        try:
            when = parsedate_to_datetime(re.sub(r"\s+", " ", tag("pubDate")).strip())
        except (TypeError, ValueError, IndexError):
            continue
        if when.tzinfo is None:
            continue
        kst = when.astimezone(KST)
        seen.add(key)
        items.append({
            "key": key, "url": link.split("?")[0], "title": title[:240], "desc": desc[:500],
            "day": kst.date(), "when": kst.strftime("%Y-%m-%d %H:%M KST"),
        })
    if not items:
        raise FeedError("파싱 실패", "날짜와 허용 주소가 있는 항목이 없음")
    return items


def parse_trendforce(text: str) -> list[dict]:
    if "presscenter/news/" not in text:
        raise FeedError("파싱 실패", "트렌드포스 목록 형식이 아님")
    items: list[dict] = []
    seen: set[str] = set()
    for matched in re.finditer(r'href="(/presscenter/news/(\d{8})-(\d+)\.html)"', text):
        if matched.group(3) in seen:
            continue
        raw_day = matched.group(2)
        try:
            day = datetime.strptime(raw_day, "%Y%m%d").date()
        except ValueError:
            continue
        seg = text[matched.end():matched.end() + 700]
        title = unescape(re.sub(r"<[^>]+>", " ", seg))
        title = re.sub(r"\s+", " ", title)
        title = re.split(rf"\s\d{{1,2}} (?:{_MONTHS}) \d{{4}}\b", title)[0]
        title = _sanitize(title.lstrip("> "))
        if len(title) < 8:
            continue
        url = "https://www.trendforce.com" + matched.group(1)
        seen.add(matched.group(3))
        items.append({
            "key": canon_url(url), "url": url, "title": title[:240], "desc": "",
            "day": day, "when": day.isoformat() + " (날짜만, 시각 없음)",
        })
    if not items:
        raise FeedError("파싱 실패", "날짜가 있는 보도자료 항목이 없음")
    return items


def _classify(item: dict) -> str:
    title = item["title"]
    if EXCLUDE_TITLE_RE.search(title):
        return "skip"
    if HBM_TITLE_RE.search(title):
        return "include"
    if EARN_TITLE_RE.search(title) or CAP_TITLE_RE.search(title):
        return "body"
    return "skip"


def _body_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return _sanitize(unescape(text))[:15000]


def _topic_line(item: dict, kind: str) -> str:
    if kind == "include":
        return "제목의 HBM, 계약, 생산능력"
    if CAP_TITLE_RE.search(item["title"]) and not EARN_TITLE_RE.search(item["title"]):
        return "생산능력 제목. 본문에서 HBM 또는 LTA 단어를 확인. HBM만인지는 단정하지 않음"
    return "실적 제목. 본문에서 HBM 또는 LTA 단어를 확인. 수치 변경은 단정하지 않음"


def _year_line(text: str, when: str) -> str:
    years = mentioned_years(text)
    if not years:
        return "전망 연도 추출 안 됨. 보도일을 전망 연도로 바꾸지 않음."
    pub = when[:10]
    shown = ", ".join(years)
    if shown == pub[:4] or (len(years) == 1 and years[0] in pub):
        return f"글에 적힌 연도: {shown}. 보도일과 같습니다. 전망 연도로 단정하지 않습니다."
    return f"글에 적힌 연도: {shown}. 보도일 {pub} 과 다릅니다."


def _outlook_body(stamp: str, item: dict, src: dict, claim: dict | None, prior: dict | None) -> tuple[str, str]:
    text = (item["title"] + " " + item.get("desc", "")).strip()
    status = f"신규 자료, 변경 여부 검토 필요. {src['label']} 보도일 {item['when']}. 제목: {item['title']}. {_year_line(text, item['when'])}"
    title = REVIEW_TITLE
    if claim and prior:
        unit = _unit_label(claim["unit"])
        status += (
            f" 비교 가능한 수치: 이전 {prior['num']:g}{unit} (보도일 {prior['date']}) / 현재 {claim['num']:g}{unit} (보도일 {item['when']}). "
            f"같은 출처, 전망 연도 {claim['year']}, 단위 {unit}, {_mix_label(claim)}. 확정 개정이나 공급 결론이 아닙니다."
        )
        if claim["num"] != prior["num"]:
            title = COMPARE_TITLE
    elif claim:
        status += f" 이번 글에서만 읽힌 값: {claim['num']:g}{_unit_label(claim['unit'])}, 전망 연도 {claim['year']}, {_mix_label(claim)}. 비교할 같은 조건의 예전 값이 없습니다."
    else:
        status += " 단위 추출 안 됨. 블렌디드/동일 세대 구분 추출 안 됨."
    lines = [
        f"{stamp} | HBMASP",
        "이게 뭐 알림: 005930 삼성전자와 000660 SK하이닉스 HBM 전망, 장기공급, 생산능력 공개 자료를 사람이 검토하라는 알림입니다. 매매 신호가 아닙니다.",
        f"지금 결과/상태: {status}",
        "지금 할 일: 원문을 열고 직접 확인하세요. 새 전망인지 같은 글의 재게재인지. 보도일과 전망 연도. 단위가 Gb인지 GB인지, 없으면 추정하지 말 것. 세대와 블렌디드인지 동일 세대인지. 공급 문장이 HBM만인지 전체 생산능력인지. 가격이 확정가인지 합의 범위 안의 시장가인지.",
        "나중에: 다음 평일 19:00에 새 자료만 다시 봅니다. 같은 글은 다시 보내지 않습니다.",
        "익절/줄일 때: 이 알림으로 보유를 줄이거나 늘리지 마세요.",
        "하지 말 것: 새 글을 전망 개정 확정이나 공급 긴축으로 읽기. 제3자 전망과 회사 발표를 한 사실로 합치기. 없는 단위나 세대 차이를 메우기.",
        f"주제: {item.get('topic')}",
        f"출처: {item['url']}",
    ]
    return title, "\n".join(lines)


def _ends_cfg(ocfg: dict) -> dict:
    return {"name": "hbmasp", "title_prefix": "[HBMASP]", "ends_kst": ocfg["ends_kst"], "ends_note": ocfg["ends_note"]}


def _with_ends(message: str, ocfg: dict) -> str:
    line = f"꺼지는 때: {ocfg['ends_kst']} KST ({ocfg['ends_note']})"
    return message if line in message else message.rstrip() + "\n" + line


def http_get(url: str) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        final = resp.geturl()
        raw = resp.read(400_000)
    return final, raw.decode("utf-8", "replace")


def _load_items(src: dict, fetch) -> list[dict]:
    allowed = {h.lower() for h in src["allowed_hosts"]}
    try:
        final, text = fetch(src["list_url"])
    except FeedError:
        raise
    except Exception as exc:
        raise FeedError("조회 실패", f"목록 조회 실패: {exc}") from exc
    if _host(final) not in allowed:
        raise FeedError("파싱 실패", "허용되지 않은 주소로 이동")
    if src["format"] == "trendforce_html":
        items = parse_trendforce(text)
    else:
        items = parse_rss(text, allowed)
    fresh = [it for it in items if _host(it["url"]) in allowed]
    if not fresh:
        raise FeedError("파싱 실패", "허용된 항목이 없음")
    return fresh


def _filter_dates(items: list[dict], today, stale_days: int) -> list[dict]:
    kept = [it for it in items if it["day"] <= today + timedelta(days=2)]
    if not kept:
        raise FeedError("파싱 실패", "날짜가 없거나 미래 날짜뿐")
    newest = max(it["day"] for it in kept)
    if newest < today - timedelta(days=stale_days):
        raise FeedError("파싱 실패", f"최신 보도일 {newest.isoformat()} 이 오래됨")
    return kept


def run_outlook(now: datetime, cfg: dict, state: dict, *, dry: bool, fetch, send) -> int:
    """Human-review pass. Mutates state only when dry is False and a seed, send, or ops day is recorded."""
    now = now.astimezone(KST)
    ocfg = cfg.get("outlook") or {}
    if not ocfg.get("sources") or not ocfg.get("ends_kst"):
        print("outlook config missing; silent")
        return 2
    try:
        end = datetime.strptime(ocfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        print("outlook ends_kst unparseable; silent")
        return 2
    if now > end:
        print("outlook ended; silent")
        return 0
    if now.weekday() >= 5:
        print("weekend; outlook silent")
        return 0
    work = copy.deepcopy(state)
    box = work.setdefault("outlook", {"sources": {}})
    box.setdefault("sources", {})
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    changed = False
    code = 0
    ends_cfg = _ends_cfg(ocfg)
    stale_days = int(ocfg.get("stale_days") or 120)
    max_send = int(ocfg.get("max_send") or 3)

    def remember(slot: dict, item: dict, claim: dict | None) -> None:
        _mark_seen(slot, item)
        if claim:
            slot.setdefault("claims", {})[_claim_key(claim)] = {"num": claim["num"], "date": item["when"], "url": item["url"]}

    for src in ocfg["sources"]:
        sid = src["id"]
        slot = box["sources"].setdefault(sid, _blank_source())
        label = {"trendforce": "트렌드포스", "samsung": "삼성뉴스룸", "skhynix": "하이닉스뉴스룸"}.get(sid, sid)
        try:
            items = _filter_dates(_load_items(src, fetch), today, stale_days)
        except FeedError as exc:
            print(f"outlook {sid} {exc.kind}: {exc.detail}")
            if dry:
                print(f"DRY RUN {_ops_title(label, exc.kind)}")
                continue
            if slot.get("ops_day") != today.isoformat():
                message = _with_ends(body(
                    stamp,
                    "HBM 공개 자료 목록 확인 실패 안내입니다. 매매 신호가 아닙니다.",
                    f"{src['label']} {exc.detail}. 빈 목록을 전망 유지로 읽지 않았습니다.",
                    "다음 평일 19:00에 다시 봅니다. 이 출처는 시드되기 전에는 과거 글을 보내지 않습니다.",
                    "조회가 살아난 뒤 첫 성공은 시드만 하고, 그 다음 새 글부터 알립니다.",
                    "실패 알림으로 보유를 줄이지 마세요.",
                    "실패를 가격 0, 계약 없음, 공급 완화로 읽기.",
                ), ocfg)
                try:
                    send(_ops_title(label, exc.kind), message, 3, ends_cfg, None)
                    slot["ops_day"] = today.isoformat()
                    changed = True
                except Exception as pub_exc:
                    print(f"ops publish failed: {pub_exc}", file=sys.stderr)
                    code = 1
            continue
        if not slot.get("seeded"):
            if dry:
                print(f"DRY RUN seed {sid} {len(items)} no send")
                continue
            for item in items:
                remember(slot, item, parse_claim(item["title"] + " " + item.get("desc", "")))
            slot["seeded"] = True
            slot["last_ok"] = today.isoformat()
            changed = True
            print(f"outlook seed {sid} {len(items)} no send")
            continue
        fresh: list[dict] = []
        titles_now: set[str] = set()
        body_fail = False
        for item in items:
            title_key = canon_title(item["title"])
            if _seen(slot, item) or title_key in titles_now:
                if not _seen(slot, item) and not dry:
                    remember(slot, item, None)
                    changed = True
                continue
            titles_now.add(title_key)
            kind = _classify(item)
            if kind == "skip":
                if not dry:
                    remember(slot, item, parse_claim(item["title"] + " " + item.get("desc", "")))
                    changed = True
                continue
            if kind == "body":
                try:
                    final, html = fetch(item["url"])
                except Exception as exc:
                    print(f"outlook body {sid} 조회 실패: {exc}")
                    body_fail = True
                    continue
                if _host(final) not in {h.lower() for h in src["allowed_hosts"]}:
                    print(f"outlook body {sid} 파싱 실패: 허용되지 않은 주소")
                    body_fail = True
                    continue
                if not BODY_OK_RE.search(_body_text(html)):
                    if not dry:
                        remember(slot, item, None)
                        changed = True
                    continue
                item["topic"] = _topic_line(item, kind)
            else:
                item["topic"] = _topic_line(item, kind)
            fresh.append(item)
        if body_fail and not dry and slot.get("ops_day") != today.isoformat():
            message = _with_ends(body(
                stamp,
                "HBM 공개 자료 본문 확인 실패 안내입니다. 매매 신호가 아닙니다.",
                f"{src['label']} 실적 또는 생산능력 제목의 본문을 읽지 못했습니다. 관련 있다고 보내지 않았습니다.",
                "다음 평일 19:00에 그 글만 다시 읽습니다.",
                "본문이 없으면 전망 변경으로 올리지 않습니다.",
                "실패 알림으로 보유를 줄이지 마세요.",
                "본문 실패를 HBM 무관 또는 가격 0으로 읽기.",
            ), ocfg)
            try:
                send(_ops_title(label, "조회 실패"), message, 3, ends_cfg, None)
                slot["ops_day"] = today.isoformat()
                changed = True
            except Exception as pub_exc:
                print(f"ops publish failed: {pub_exc}", file=sys.stderr)
                code = 1
        sent_n = 0
        for item in fresh:
            if sent_n >= max_send:
                print(f"outlook {sid} hold extra item for next run")
                break
            claim = parse_claim(item["title"] + " " + item.get("desc", ""))
            prior = (slot.get("claims") or {}).get(_claim_key(claim)) if claim else None
            title, message = _outlook_body(stamp, item, src, claim, prior)
            message = _with_ends(message, ocfg)
            if dry:
                print("DRY RUN")
                print(f"title={title}")
                print(message)
                sent_n += 1
                continue
            try:
                send(title, message, 3, ends_cfg, item["key"])
            except Exception as exc:
                print(f"publish failed outlook: {exc}", file=sys.stderr)
                code = 1
                break
            remember(slot, item, claim)
            changed = True
            sent_n += 1
        if not dry:
            slot["last_ok"] = today.isoformat()
            changed = True
        if sent_n == 0 and not body_fail:
            print(f"outlook {sid} no new review item")
    if changed and not dry:
        work["updated_at"] = stamp
        state.clear()
        state.update(work)
    return code


def outlook_cli(dry: bool) -> int:
    now = datetime.now(KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, default_state())
    before = STATE_PATH.read_bytes() if STATE_PATH.exists() else None

    def live_send(title: str, message: str, priority: int, ends_cfg: dict, dedup_key: str | None) -> None:
        ntfy = helper("ntfy_util")
        server = ntfy.resolve_server(cfg.get("ntfy_server"))
        topic = ntfy.route_topic("cwally-port", title)
        print(f"channel={topic} title={title}")
        print(message)
        ntfy.publish("cwally-port", title, message, priority, ["memo"], server, config=ends_cfg, dedup_key=dedup_key)

    code = run_outlook(now, cfg, state, dry=dry, fetch=http_get, send=live_send)
    if dry:
        after = STATE_PATH.read_bytes() if STATE_PATH.exists() else None
        if after != before:
            print("DRY RUN state mutated", file=sys.stderr)
            return 1
        print("DRY RUN no ntfy, no state")
        return code
    loaded = json.loads(before.decode("utf-8")) if before else default_state()
    if state != loaded:
        atomic_save(STATE_PATH, state)
        print("state updated")
    else:
        print("outlook state unchanged")
    return code


def outlook_selftest(check) -> None:
    """Offline fixtures only. urlopen is blocked for the duration."""
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ocfg = cfg["outlook"]
    blob = json.dumps(ocfg, ensure_ascii=False)
    check("outlook config: no hardcoded 121/122/3.7/Goldman baseline",
          all(s not in blob for s in ("121", "122", "3.7", "골드만", "100~160")), True)
    check("outlook ends 2027-11-30 19:00 on the 19:00 slot, morning ends stay 2027-02-06",
          (ocfg["ends_kst"], ocfg["schedule_kst"], cfg["ends_kst"], [s["id"] for s in ocfg["sources"]]),
          ("2027-11-30 19:00", "19:00", "2027-02-06 09:00", ["trendforce", "samsung", "skhynix"]))
    urls = {s["id"]: s["list_url"] for s in ocfg["sources"]}
    pub = "Mon, 09 Nov 2026 02:00:00 +0000"
    monday = datetime(2026, 11, 9, 19, 0, tzinfo=KST)

    def rss(items: list[dict]) -> str:
        rows = ["<rss><channel>"]
        for it in items:
            rows.append(
                f"<item><title>{it['title']}</title><link>{it['link']}</link>"
                f"<guid>https://bit.ly/not-canonical</guid><pubDate>{it.get('pub', pub)}</pubDate>"
                f"<description>{it.get('desc', '')}</description></item>"
            )
        rows.append("</channel></rss>")
        return "".join(rows)

    def tf(rows: list[tuple[str, str, str]]) -> str:
        return "".join(
            f'<a href="/presscenter/news/{ymd}-{idx}.html"><strong>{title}</strong></a> 9 November 2026 '
            for ymd, idx, title in rows
        )

    pages: dict[str, tuple[str, str]] = {}
    calls: list[str] = []

    def fetch(url: str):
        calls.append(url)
        if url not in pages:
            raise RuntimeError("down " + url)
        return pages[url]

    sent: list[tuple] = []

    def send(*args):
        sent.append(args)

    def go(state: dict, when: datetime | None = None, dry: bool = False):
        sent.clear()
        calls.clear()
        code = run_outlook(when or monday, cfg, state, dry=dry, fetch=fetch, send=send)
        return code, [row[0] for row in sent], [row[1] for row in sent]

    def base():
        pages.clear()
        pages[urls["trendforce"]] = (urls["trendforce"], tf([("20261109", "1", "DRAM contract prices rise, Says TrendForce")]))
        pages[urls["samsung"]] = (urls["samsung"], rss([{"title": "Samsung Ranks No. 5 in Interbrand 2026", "link": "https://news.samsung.com/global/interbrand-2026"}]))
        pages[urls["skhynix"]] = (urls["skhynix"], rss([{"title": "SK hynix Launches a Ventures Office", "link": "https://news.skhynix.com/en/ventures-office"}]))

    blocked = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
    try:
        check("parse: year and blended YoY are one claim", (parse_claim("2027 blended HBM ASP +10% YoY.") or {}).get("year"), "2027")
        check("parse: missing year is not a claim", parse_claim("blended HBM ASP +10% YoY.") is None, True)
        check("parse: range, hedge, missing unit, Gb with GB are not claims",
              [parse_claim(s) is None for s in (
                  "HBM4 $3-5/Gb in 2027.",
                  "about 100% blended HBM ASP YoY in 2027.",
                  "HBM4 ASP 4 in 2027.",
                  "HBM4 $4/Gb and $4/GB in 2027.",
              )], [True, True, True, True])
        check("canon url drops query, slash, and scheme",
              canon_url("https://news.samsung.com/global/a?utm=1"), canon_url("http://news.samsung.com/global/a/"))
        dup = parse_rss(rss([
            {"title": "Same", "link": "https://news.samsung.com/global/a?utm=1"},
            {"title": "Same", "link": "https://news.samsung.com/global/a"},
        ]), {"news.samsung.com"})
        check("rss canonical duplicate collapses to one item and ignores bit.ly guid", len(dup), 1)

        base()
        state: dict = {}
        code, titles, _ = go(state)
        check("initial seed sends nothing", (code, titles, state["outlook"]["sources"]["trendforce"]["seeded"], state["outlook"]["sources"]["samsung"]["seeded"]), (0, [], True, True))
        code, titles, _ = go(state)
        check("same lists again stay silent", (code, titles), (0, []))

        pages[urls["trendforce"]] = (urls["trendforce"], tf([
            ("20261109", "1", "DRAM contract prices rise, Says TrendForce"),
            ("20261109", "2", "2027 blended HBM ASP +10% YoY, Says TrendForce"),
        ]))
        code, titles, msgs = go(state)
        check("new third-party article is review, not a confirmed revision", titles, [REVIEW_TITLE])
        msg = msgs[0]
        check("message separates source date and forecast year",
              ("보도일 2026-11-09" in msg, "글에 적힌 연도: 2027" in msg, "보도일 2027" not in msg, "제3자 전망" in msg),
              (True, True, True, True))
        status = next(line for line in msg.splitlines() if line.startswith("지금 결과"))
        check("status does not call it a confirmed revision or tighter supply",
              ("신규 자료, 변경 여부 검토 필요" in status, "공급 긴축" not in status, "개정 확정" not in status), (True, True, True))
        check("no middle dot or em dash, checklist present, under 3500 bytes",
              (any(ch in msg for ch in "\u00b7\u2014"), "Gb" in msg, "생산능력" in msg, len(msg.encode("utf-8")) < 3500),
              (False, True, True, True))

        pages[urls["trendforce"]] = (urls["trendforce"], tf([
            ("20261109", "1", "DRAM contract prices rise, Says TrendForce"),
            ("20261109", "2", "2027 blended HBM ASP +10% YoY, Says TrendForce"),
            ("20261109", "3", "2027 blended HBM ASP +12% YoY, Says TrendForce"),
        ]))
        code, titles, msgs = go(state)
        check("later comparable figure is quantified and still not a verdict", titles, [COMPARE_TITLE])
        status = next(line for line in msgs[0].splitlines() if line.startswith("지금 결과"))
        check("compare shows prior and current with the same year and unit",
              ("이전 10%" in status, "현재 12%" in status, "블렌디드" in status, "개정 확정" not in status, "공급 긴축" not in status),
              (True, True, True, True, True))

        pages[urls["skhynix"]] = (urls["skhynix"], rss([
            {"title": "SK hynix Launches a Ventures Office", "link": "https://news.skhynix.com/en/ventures-office"},
            {"title": "SK hynix Starts HBM Production Base in Indiana", "link": "https://news.skhynix.com/en/hbm-indiana"},
            {"title": "SK hynix Starts HBM Production Base in Indiana", "link": "https://news.skhynix.com/en/hbm-indiana-copy"},
            {"title": "Tech Note: Why GPUs wait", "link": "https://news.skhynix.com/en/tech-note", "desc": "HBM sits next to the GPU."},
            {"title": "DRAM contract prices fall hard", "link": "https://news.skhynix.com/en/dram-only"},
            {"title": "Galaxy camera feature for smartphones", "link": "https://news.skhynix.com/en/phone"},
            {"title": "Q3 2026 Earnings Conference Call Invitation", "link": "https://news.skhynix.com/en/invite"},
        ]))
        code, titles, msgs = go(state)
        check("issuer HBM title once, duplicates and off-topic titles dropped", titles, [REVIEW_TITLE])
        check("issuer label, and invite was not fetched",
              ("발행사 공식" in msgs[0], "https://news.skhynix.com/en/invite" not in calls), (True, True))

        pages[urls["samsung"]] = (urls["samsung"], rss([
            {"title": "Samsung Ranks No. 5 in Interbrand 2026", "link": "https://news.samsung.com/global/interbrand-2026"},
            {"title": "Samsung Electronics Announces Third Quarter 2026 Results", "link": "https://news.samsung.com/global/q3-results"},
        ]))
        pages["https://news.samsung.com/global/q3-results"] = (
            "https://news.samsung.com/global/q3-results",
            "<html><body>Memory discussion covers HBM4. 2027 blended HBM ASP +12% YoY. Galaxy phones also shipped.</body></html>",
        )
        code, titles, msgs = go(state)
        check("generic earnings title counts only after body HBM check, body number is not a revision", titles, [REVIEW_TITLE])
        status = next(line for line in msgs[0].splitlines() if line.startswith("지금 결과"))
        check("body figure is not treated as a parsed change, and the timestamp is KST",
              ("현재 12%" not in status, "실적 제목" in msgs[0], "보도일 2026-11-09 11:00 KST" in status), (True, True, True))
        pages[urls["samsung"]] = (urls["samsung"], rss([
            {"title": "Samsung Ranks No. 5 in Interbrand 2026", "link": "https://news.samsung.com/global/interbrand-2026"},
            {"title": "Samsung Electronics Announces Third Quarter 2026 Results", "link": "https://news.samsung.com/global/q3-results"},
            {"title": "Samsung Electronics Announces Earnings Guidance for a Phone Quarter", "link": "https://news.samsung.com/global/phone-guide"},
        ]))
        pages["https://news.samsung.com/global/phone-guide"] = (
            "https://news.samsung.com/global/phone-guide",
            "<html><body>Galaxy smartphone camera only.</body></html>",
        )
        before_calls = len(calls)
        code, titles, _ = go(state)
        check("smartphone earnings body is not an HBM alert", titles, [])
        code, titles, _ = go(state)
        check("irrelevant earnings body is not fetched again", ("https://news.samsung.com/global/phone-guide" in calls), False)

        pages[urls["trendforce"]] = (urls["trendforce"], tf([
            ("20261109", "2", "2027 blended HBM ASP +10% YoY, Says TrendForce"),
            ("20261109", "4", "HBM4 $8/GB in 2027, Says TrendForce"),
            ("20261109", "5", "HBM4 quoted $3-5/Gb in 2027, Says TrendForce"),
        ]))
        code, titles, msgs = go(state)
        joined = " ".join(msgs)
        check("GB price and a range are separate review links, not a Gb comparison",
              (titles, "이전 " in joined, "USD/GB" in joined), ([REVIEW_TITLE, REVIEW_TITLE], False, True))

        hold = dict(state)
        pages[urls["skhynix"]] = (urls["skhynix"], rss(
            [{"title": "SK hynix Launches a Ventures Office", "link": "https://news.skhynix.com/en/ventures-office"}]
            + [{"title": f"HBM supply note {n}", "link": f"https://news.skhynix.com/en/note-{n}"} for n in range(1, 5)]
        ))
        code, titles, _ = go(state)
        check("at most 3 new reviews per source per run", len(titles), 3)
        code, titles, _ = go(state)
        check("held item sends on the next run", len(titles), 1)

        stale: dict = {}
        pages[urls["trendforce"]] = (urls["trendforce"], tf([("20240102", "9", "2027 blended HBM ASP +10% YoY, Says TrendForce")]))
        base_sam = pages[urls["samsung"]]
        pages[urls["samsung"]] = (urls["samsung"], "<html><title>not a feed</title></html>")
        pages[urls["skhynix"]] = (urls["skhynix"], "<rss><channel></channel></rss>")
        code, titles, _ = go(stale)
        check("stale, html, and empty feeds fail closed and do not seed",
              (any("파싱 실패" in t for t in titles), len(titles) == 3,
               stale["outlook"]["sources"]["trendforce"]["seeded"],
               stale["outlook"]["sources"]["samsung"]["seeded"],
               stale["outlook"]["sources"]["skhynix"]["seeded"]),
              (True, True, False, False, False))
        code, titles, _ = go(stale)
        check("same-day source failure does not repeat", titles, [])

        down: dict = {}
        base()
        real_tf = pages[urls["trendforce"]]

        def fetch_sk_down(url: str):
            calls.append(url)
            if url == urls["skhynix"]:
                raise RuntimeError("timeout")
            if url not in pages:
                raise RuntimeError("down " + url)
            return pages[url]

        def go_fetch(state: dict, fetch_fn, when=None):
            sent.clear()
            calls.clear()
            code = run_outlook(when or monday, cfg, state, dry=False, fetch=fetch_fn, send=send)
            return code, [row[0] for row in sent]

        code, titles = go_fetch(down, fetch_sk_down)
        check("one source down seeds the others and does not send a review",
              (any("조회 실패" in t for t in titles), REVIEW_TITLE not in titles,
               down["outlook"]["sources"]["trendforce"]["seeded"], down["outlook"]["sources"]["skhynix"]["seeded"]),
              (True, True, True, False))
        pages[urls["trendforce"]] = (urls["trendforce"], tf([
            ("20261109", "1", "DRAM contract prices rise, Says TrendForce"),
            ("20261109", "8", "HBM contract language changed, Says TrendForce"),
        ]))
        code, titles = go_fetch(down, fetch_sk_down)
        check("healthy source still reports a new article while another source is down", titles, [REVIEW_TITLE])
        check("failed source is still unseeded", down["outlook"]["sources"]["skhynix"]["seeded"], False)
        pages[urls["skhynix"]] = (urls["skhynix"], rss([
            {"title": "SK hynix Launches a Ventures Office", "link": "https://news.skhynix.com/en/old-1"},
            {"title": "Older press note", "link": "https://news.skhynix.com/en/old-2"},
        ]))
        code, titles = go_fetch(down, fetch)
        check("source recovery seeds history and does not flood", (titles, down["outlook"]["sources"]["skhynix"]["seeded"]), ([], True))
        pages[urls["skhynix"]] = (urls["skhynix"], rss([
            {"title": "SK hynix Launches a Ventures Office", "link": "https://news.skhynix.com/en/old-1"},
            {"title": "Older press note", "link": "https://news.skhynix.com/en/old-2"},
            {"title": "SK hynix signs a long-term HBM supply agreement", "link": "https://news.skhynix.com/en/new-lta"},
        ]))
        code, titles = go_fetch(down, fetch)
        check("only the post-recovery article is sent", titles, [REVIEW_TITLE])

        pages[urls["samsung"]] = (urls["samsung"], rss([{"title": "HBM note", "link": "https://bit.ly/leak"}]))
        bad: dict = {}
        pages[urls["trendforce"]] = real_tf
        pages[urls["skhynix"]] = (urls["skhynix"], rss([{"title": "Older press note", "link": "https://news.skhynix.com/en/old-2"}]))
        code, titles, _ = go(bad)
        check("off-allowlist link is a parse failure, not a signal",
              (any("삼성뉴스룸" in t and "파싱 실패" in t for t in titles), bad["outlook"]["sources"]["samsung"]["seeded"]),
              (True, False))
        evil: dict = {}
        pages[urls["samsung"]] = ("https://evil.example/moved", rss([{"title": "HBM note", "link": "https://news.samsung.com/global/q"}]))
        code, titles, _ = go(evil)
        check("redirect off the allowlist fails closed",
              (any("허용되지 않은 주소" in m for m in _), evil["outlook"]["sources"]["samsung"]["seeded"]),
              (True, False))

        dry_state: dict = {}
        base()
        code, titles, _ = go(dry_state, dry=True)
        check("dry seed does not send or write state", (titles, dry_state), ([], {}))
        seeded = {"outlook": {"sources": {"trendforce": {"seeded": True, "seen": [], "claims": {}, "ops_day": None},
                                           "samsung": {"seeded": True, "seen": ["u:x"], "claims": {}, "ops_day": None},
                                           "skhynix": {"seeded": True, "seen": ["u:y"], "claims": {}, "ops_day": None}}}}
        pages[urls["trendforce"]] = (urls["trendforce"], tf([("20261109", "7", "HBM outlook note, Says TrendForce")]))
        snapshot = copy.deepcopy(seeded)
        code, titles, _ = go(seeded, dry=True)
        check("dry new item does not send or mutate state", (titles, seeded), ([], snapshot))

        quiet: dict = {}
        code, titles, _ = go(quiet, when=datetime(2026, 11, 8, 19, 0, tzinfo=KST))
        check("Sunday stays silent and does not fetch", (code, titles, calls, quiet), (0, [], [], {}))
        code, titles, _ = go(quiet, when=datetime(2027, 12, 1, 19, 0, tzinfo=KST))
        check("after outlook end stays silent", (code, titles, calls), (0, [], []))
        late_pub = "Tue, 30 Nov 2027 02:00:00 +0000"
        pages[urls["trendforce"]] = (urls["trendforce"], tf([("20271130", "1", "DRAM contract prices rise, Says TrendForce")]))
        pages[urls["samsung"]] = (urls["samsung"], rss([{"title": "Samsung Ranks No. 5 in Interbrand 2026", "link": "https://news.samsung.com/global/interbrand-2026", "pub": late_pub}]))
        pages[urls["skhynix"]] = (urls["skhynix"], rss([{"title": "SK hynix Launches a Ventures Office", "link": "https://news.skhynix.com/en/ventures-office", "pub": late_pub}]))
        end_state: dict = {}
        code, _, _ = go(end_state, when=datetime(2027, 11, 30, 19, 0, tzinfo=KST))
        check("last outlook day still runs", end_state.get("outlook", {}).get("sources", {}).get("trendforce", {}).get("seeded"), True)
        _ = base_sam, before_calls, hold
    finally:
        urllib.request.urlopen = blocked


def selftest() -> int:
    """Offline: stubbed ntfy_util and naver_api (no network, no publish), temp state file; the real state is untouched."""
    import tempfile

    global STATE_PATH, helper
    real_state, real_helper = STATE_PATH, helper
    before = real_state.read_bytes() if real_state.exists() else None
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    sent: list = []
    feed = {"items": [], "fail": False}
    fails = []

    class Ntfy:
        @staticmethod
        def resolve_server(x):
            return "https://ntfy.invalid"

        @staticmethod
        def publish(topic, title, msg, pri=3, tags=None, server=None):
            sent.append((topic, title, msg, pri))

        @staticmethod
        def parse_ends_kst(raw):
            return real_helper("ntfy_util").parse_ends_kst(raw)

    class Naver:
        @staticmethod
        def notices(code, size=30):
            if feed["fail"]:
                raise RuntimeError("HTTP 503")
            return list(feed["items"]) if code == "005930" else []

    def check(label, got, want):
        good = got == want
        print(f"{'ok  ' if good else 'FAIL'} {label}: got={got!r} want={want!r}")
        if not good:
            fails.append(label)

    def at(d: str) -> datetime:
        return datetime.fromisoformat(d).replace(hour=9, minute=0, tzinfo=KST)

    def titles(d: str) -> list:
        sent.clear()
        run(at(d))
        return [t for _, t, _, _ in sent]

    with tempfile.TemporaryDirectory() as tmp:
        STATE_PATH = Path(tmp) / "hbmasp.json"
        helper = lambda name: {"ntfy_util": Ntfy, "naver_api": Naver}[name]  # noqa: E731
        try:
            base = "[HBMASP] 005930/000660 기준 유지 | 제시가를 확정가로 보지 말 것"
            check("config: window to 2027-02-06, years 2026 and 2027, ends 2027-02-06 09:00",
                  (cfg["expire_date"], cfg["year_guard"], cfg["ends_kst"]), ("2027-02-06", [2026, 2027], "2027-02-06 09:00"))
            check("config: ends_kst parses", str(Ntfy.parse_ends_kst(cfg["ends_kst"])[0])[:16], "2027-02-06 09:00")
            check("Mon 2026-11-16 (after the old 11/13 end): weekly baseline once", titles("2026-11-16"), [base])
            check("Tue 2026-11-17: nothing new, silent", titles("2026-11-17"), [])
            check("Mon 2027-01-04 (year 2027 allowed): baseline", titles("2027-01-04"), [base])
            check("Mon 2027-02-01 (last Monday in window): baseline", titles("2027-02-01"), [base])
            msg = sent[0][2]
            check("baseline lists the 10/26 quote and the Eugene estimate",
                  (cfg["baseline"]["dt_1026"] in msg, cfg["baseline"]["eugene"] in msg, "계약가 아님" in msg, "3.31달러/Gb" in msg), (True, True, True, True))
            check("baseline under 3,800 bytes (ntfy 4,096 with the end line)", len(msg.encode("utf-8")) < 3800, True)
            feed["items"] = [{"id": "1", "title": "삼성전자(주) 단일판매ㆍ공급계약체결", "datetime": "2027-02-05 08:00", "url": "u"},
                             {"id": "2", "title": "삼성전자(주) 자기주식처분결과보고서", "datetime": "2027-02-05 08:00", "url": "u"}]
            got = titles("2027-02-05")
            check("Fri 2027-02-05: matching filing once (unrelated silent)", got, ["[HBMASP] 005930/000660 새 공시 | 제시가와 확정가 구분"])
            check("filing message has no middle dot / em-dash", any(c in sent[0][2] for c in "\u00b7\u2014"), False)
            check("same filing again silent", titles("2027-02-05"), [])
            feed["items"] = [dict(feed["items"][0], id="3", datetime="2027-02-08 08:00")]
            check("Sun 2027-02-07 after expire_date 2027-02-06 (inclusive; no weekday run on 2/6, ntfy also stops at ends_kst): silent",
                  titles("2027-02-07"), [])
            check("Mon 2027-02-08: silent (no baseline, no filing)", titles("2027-02-08"), [])
            check("Mon 2028-01-03: silent (year outside guard)", titles("2028-01-03"), [])
            check("2026-10-06 before active_from: silent", titles("2026-10-06"), [])
            feed["fail"] = True
            sent.clear()
            run(at("2027-01-05"))
            check("feed down: one cwally-ops notice", [(tp, t) for tp, t, _, _ in sent], [("cwally-ops", "[HBMASP] 005930/000660 공시 조회 실패 | 매매 신호 아님")])
            check("feed down again same day: silent", titles("2027-01-05"), [])
            check("year_ok int and list", (year_ok({"year_guard": 2026}, 2027), year_ok({"year_guard": [2026, 2027]}, 2027)), (False, True))
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            data["outlook"] = {"sources": {"trendforce": {"seeded": True, "seen": ["u:keep"]}}}
            STATE_PATH.write_text(json.dumps(data), encoding="utf-8")
            feed["fail"] = False
            feed["items"] = [{"id": "9", "title": "삼성전자(주) 단일판매ㆍ공급계약체결", "datetime": "2027-02-05 18:00", "url": "u9"}]
            titles("2027-02-05")
            kept = json.loads(STATE_PATH.read_text(encoding="utf-8"))["outlook"]["sources"]["trendforce"]["seen"]
            check("morning save keeps outlook seen keys", kept, ["u:keep"])
            outlook_selftest(check)
        finally:
            STATE_PATH, helper = real_state, real_helper
    check("real state file untouched", (real_state.read_bytes() if real_state.exists() else None) == before, True)
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 0 if not fails else 1


def main() -> int:
    if sys.argv[1:] == ["selftest"]:  # same spelling as the other monitors
        return selftest()
    if len(sys.argv) > 1 and sys.argv[1] == "outlook":
        mode = sys.argv[2] if len(sys.argv) > 2 else "auto"
        if mode not in ("auto", "dry") or len(sys.argv) > 3:
            print("usage: hbmasp_monitor.py outlook [auto|dry]", file=sys.stderr)
            return 2
        return outlook_cli(dry=(mode == "dry"))
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-confirm", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    now = datetime.now(KST)
    if args.selftest:
        return selftest()
    if args.install_confirm:
        return install_confirm(now)
    return run(now)


if __name__ == "__main__":
    raise SystemExit(main())
