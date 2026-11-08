#!/usr/bin/env python3
"""009540 HD한국조선해양, 329180 HD현대중공업 공식 공시 변경 알림.

평일 19:00 k203780 슬롯. 매매 신호가 아니다. 가격, 비중, 매수 횟수는 보지 않는다.
KIND 최신 100건. KIND 가 예외(HTTP 오류, 형식 이상)일 때만 네이버 KRX 공시 100건으로 대체한다.
KIND 정상 빈 목록('조회된 결과값이 없습니다')은 공시 없음으로 보고 대체하지 않는다. OpenDART 키와 회사 IR 페이지는 쓰지 않는다.
첫 실행은 감시 시작일(window_start) 이전 공시만 시드하고, 시작일 이후 공시는 첫 실행에서도 보낸다.
modes: auto | dry (발송 없음, state 안 씀) | selftest
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secdiv_monitor as sd  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = sd.KST
NAME = "hdship"
TAG = "[HDSHIP]"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
NEED = "NEED_BODY"
DOT = "[\u318d\u00b7\u2027\u30fb\u2022]"
SKIP_RE = re.compile(r"배당락|가격제한|풍문|중대재해|생산중단|주주총회소집|기업설명회|지속가능경영|단일판매|공급계약|조회공시")
RECORD_RE = re.compile(r"주주명부\s*폐쇄|기준일\s*설정")
DIV_RE = re.compile(rf"현금\s*{DOT}?\s*현물\s*배당\s*결정|현금\s*배당\s*결정")
VUP_RE = re.compile(r"기업\s*가치\s*제고")
RETURN_RE = re.compile(r"주주\s*환원|배당\s*정책")
RPT_RE = re.compile(r"분기보고서|반기보고서|사업보고서|감사보고서\s*제출")
STRUCT_RE = re.compile(r"손익구조")
OUTLOOK_RE = re.compile(r"영업실적\s*등\s*에\s*대한\s*전망|영업실적\s*전망")
PROV_RE = re.compile(rf"영업\s*\(?\s*잠정\s*\)?\s*실적")
FORM_RE = re.compile(r"신규\s*시설\s*투자|출자\s*증권\s*취득\s*결정|타\s*법인\s*주식\s*및\s*출자")
DISC_RE = re.compile(r"자율\s*공시|공정\s*공시")
STRONG_RE = re.compile(r"출자|합작|신규\s*시설|조선소")
GEO = (
    ("미국", re.compile(r"미국|미합중국")),
    ("USA", re.compile(r"\bUSA\b|U\.S\.A?\.?|United States", re.I)),
    ("인도조선", re.compile(r"인도\s*조선")),
    ("인도합작", re.compile(r"인도\s*합작")),
    ("인도법인", re.compile(r"인도\s*법인")),
    ("인도현지", re.compile(r"인도\s*현지")),
    ("India", re.compile(r"\bIndia\b")),
)
YARD = (
    ("조선소", re.compile(r"조선소")),
    ("조선법인", re.compile(r"조선\s*법인")),
    ("야드", re.compile(r"야드|shipyard", re.I)),
)
GEO_LABELS = {a for a, _ in GEO}
YARD_LABELS = {a for a, _ in YARD}
BAD_CHARS = re.compile("[\u00b7\u318d\u2014\u2015]")
KIND_EMPTY = "조회된 결과값이 없습니다"
LAST_COUNTS: dict[str, tuple[int, int]] = {}  # code -> (원본 행 수, 기간 필터 후 행 수)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def names_of(co: dict) -> list[str]:
    base = [co.get("name") or "", *(co.get("aliases") or [])]
    out = []
    for n in base:
        n = (n or "").strip()
        if not n:
            continue
        out.extend((n, n + "(주)", "주식회사 " + n, "주식회사" + n))
    return sorted(set(out), key=len, reverse=True)


def empty_bucket() -> dict:
    return {"seeded_at": None, "seen": [], "title_keys": [], "seed_pending": []}


def fresh_state(cfg: dict) -> dict:
    return {
        "updated_at": None,
        "ops_day": None,
        "tickers": {co["code"]: empty_bucket() for co in cfg["companies"]},
    }


def load_state(path: Path, cfg: dict) -> dict:
    st = fresh_state(cfg)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return st
    if not isinstance(raw, dict):
        return st
    st["updated_at"] = raw.get("updated_at")
    st["ops_day"] = raw.get("ops_day")
    ticks = raw.get("tickers") if isinstance(raw.get("tickers"), dict) else {}
    for code, bucket in st["tickers"].items():
        got = ticks.get(code) if isinstance(ticks.get(code), dict) else {}
        bucket["seeded_at"] = got.get("seeded_at")
        for key in ("seen", "title_keys", "seed_pending"):
            val = got.get(key) or []
            bucket[key] = list(val) if isinstance(val, list) else []
    return st


def save_state(path: Path, st: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def show_title(t: str) -> str:
    t = re.sub(rf"\s*{DOT}\s*", ", ", t or "")
    t = re.sub(r"[\u2014\u2015]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def clean_title(raw: str, names: list[str]) -> str:
    t = re.sub(r"\s*(공시차트|주가차트).*$", "", raw or "").strip()
    changed = True
    while changed and t:
        changed = False
        for n in names:
            for pfx in (n + "(주)", "주식회사 " + n, "주식회사" + n, n):
                if pfx and t.startswith(pfx):
                    t = t[len(pfx):].lstrip(" ()")
                    changed = True
                    break
            if changed:
                break
    t = re.sub(r"\s+(유가증권시장본부|코스닥시장본부|한국거래소)$", "", t).strip()
    return t


def scrub(text: str, names: list[str]) -> str:
    out = text or ""
    for n in names:
        if n:
            out = out.replace(n, " ")
    return out


def token_labels(text: str) -> list[str]:
    found = []
    for label, rx in GEO + YARD:
        if rx.search(text or ""):
            found.append(label)
    return found


def tokens_ok(labels: list[str]) -> bool:
    have = set(labels)
    return bool(have & GEO_LABELS) and bool(have & YARD_LABELS)


def title_decision(title: str) -> str | None:
    """제목만 보고 분류. 해외투자 서식인데 문구가 제목에 없으면 NEED_BODY."""
    t = title or ""
    if SKIP_RE.search(t):
        return None
    if RECORD_RE.search(t):
        return "배당기준일" if "배당" in t else "기준일"
    if DIV_RE.search(t):
        return "현금배당"
    if VUP_RE.search(t):
        return "기업가치제고"
    if RETURN_RE.search(t):
        return "주주환원"
    if RPT_RE.search(t):
        return "정기보고서"
    if STRUCT_RE.search(t):
        return "손익구조"
    if OUTLOOK_RE.search(t):
        return "실적전망"
    if PROV_RE.search(t):
        return "잠정실적"
    if FORM_RE.search(t):
        return "해외투자문구" if tokens_ok(token_labels(t)) else NEED
    if DISC_RE.search(t) and STRONG_RE.search(t) and tokens_ok(token_labels(t)):
        return "해외투자문구"
    return None


def norm_when(raw: str) -> tuple[str, str]:
    s = str(raw or "").replace("T", " ").replace(".", "-").strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return "", ""
    day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    hm = re.search(r"(\d{2}:\d{2})", s)
    return day, (f"{day} {hm.group(1)}" if hm else day)


def in_window(day: str, since: str, until: str) -> bool:
    return bool(day) and since <= day <= until


def parse_kind_html(page: str, co: dict, since: str, until: str) -> list[dict]:
    names = names_of(co)
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page or ""):
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not acpt:
            continue
        rows += 1
        idx = row.find("openDisclsViewer")
        tm = re.search(r"title='([^']*)'", row[idx:]) if idx >= 0 else None
        if tm and tm.group(1).strip():
            title = tm.group(1).strip()
        else:
            t = sd.text_of(row)
            dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
            title = t[dm.end():].strip() if dm else t
        title = clean_title(title, names)
        whole = sd.text_of(row)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", whole)
        day, filed = norm_when(f"{dm.group(1)} {dm.group(2)}" if dm else "")
        if not in_window(day, since, until):
            continue
        ac = acpt.group(1)
        out.append({
            "id": "kind:" + ac,
            "acptno": ac,
            "title": title,
            "filed": filed,
            "src": "KIND",
            "url": sd.KIND_VIEW.format(acpt=ac),
        })
    if rows == 0 and KIND_EMPTY not in (page or ""):
        # 표만 있고 행도 '결과 없음' 문구도 없으면 차단/점검 페이지로 보고 실패 처리한다.
        raise RuntimeError("KIND 응답 형식 이상(행 0, 결과없음 문구 없음)")
    LAST_COUNTS[co["code"]] = (rows, len(out))
    return sorted(out, key=lambda r: r["id"])


def kind_list(co: dict, since: str, until: str) -> list[dict]:
    code = co["code"]
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
        "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + code, "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "",
        "searchCorpName": code, "business": "", "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = sd.http(
        sd.KIND + "/disclosure/details.do",
        data=form,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": sd.KIND + "/disclosure/details.do?method=searchDetailsMain",
        },
    )
    return parse_kind_html(page, co, since, until)


def parse_naver_rows(raw, co: dict, since: str, until: str) -> list[dict]:
    names = names_of(co)
    out = []
    if not isinstance(raw, list):
        raise RuntimeError(f"네이버 응답 형식 이상({type(raw).__name__})")
    for r in raw:
        day, filed = norm_when(str(r.get("datetime") or ""))
        nid = str(r.get("no") or r.get("bizdateSeq") or "")
        if not nid or not in_window(day, since, until):
            continue
        title = clean_title(str(r.get("title") or ""), names)
        body = sd.text_of(str(r.get("contents") or ""))
        out.append({
            "id": "naver:" + nid,
            "acptno": None,
            "title": title,
            "filed": filed,
            "src": "네이버 KRX 공시 피드",
            "url": f"https://finance.naver.com/item/news_notice.naver?code={co['code']}",
            "body": body,
        })
    LAST_COUNTS[co["code"]] = (len(raw), len(out))
    return sorted(out, key=lambda r: r["id"])


def naver_list(co: dict, since: str, until: str) -> list[dict]:
    raw = json.loads(sd.http(
        f"https://stock.naver.com/api/domestic/detail/notice?itemCode={co['code']}&page=1&pageSize=100"
    ))
    return parse_naver_rows(raw, co, since, until)


def fetch_company(co: dict, since: str, until: str) -> tuple[list[dict], str]:
    """KIND 목록이 예외 없이 오면 그 결과(0건 포함)를 쓴다. 0건은 기간 안 공시 없음이지 실패가 아니다.
    KIND 가 예외일 때만 네이버로 넘어간다. 둘 다 예외일 때만 조회 실패."""
    try:
        return kind_list(co, since, until), "KIND"
    except Exception as exc:  # noqa: BLE001
        kind_err = str(exc)
        print(f"{co['code']} KIND 목록 실패, 네이버로 대체: {kind_err[:180]}", file=sys.stderr)
    try:
        return naver_list(co, since, until), "네이버"
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"KIND {kind_err} / 네이버 {exc}") from exc


def default_body(row: dict) -> str:
    if row.get("body"):
        return str(row["body"])
    acpt = row.get("acptno")
    if not acpt:
        raise RuntimeError("본문 없음")
    text = sd.kind_body(acpt)
    if not str(text or "").strip():
        raise RuntimeError("본문 비어 있음")
    return text


def title_key(code: str, row: dict) -> str | None:
    day, _ = norm_when(row.get("filed") or "")
    title = re.sub(r"\s+", "", show_title(row.get("title") or ""))
    if not day or not title:
        return None
    return f"{code}|{day}|{title}"


def check_text(title: str, message: str) -> None:
    blob = (title or "") + "\n" + (message or "")
    if BAD_CHARS.search(blob):
        raise RuntimeError("본문에 가운데점 또는 줄표가 있음")


def build_message(stamp: str, code: str, name: str, row: dict, label: str, tokens: list[str]) -> tuple[str, str, int, str]:
    shown = show_title(row.get("title") or "")[:90] or "제목 없음"
    filed = row.get("filed") or "시각 미상"
    src = row.get("src") or "출처 미상"
    url = row.get("url") or "링크 없음"
    extra = ""
    if tokens:
        extra = " 함께 보인 문구: " + ", ".join(tokens) + ". 미국 또는 인도 조선소 출자로 단정하지 않음."
    ticker = code
    title = f"{TAG} {ticker} 공시 확인/재검토 | {label}"
    lines = [
        f"{stamp} | {code} | {name}",
        "이게 뭐 알림: 009540 HD한국조선해양, 329180 HD현대중공업 공식 공시 변경 알림. 매매 신호 아님.",
        f"지금 결과/상태: {code} {name}. {filed}. {src}. 분류 {label}. 제목: {shown}.{extra} 링크: {url}",
        "지금 할 일: 원문에서 세 가지만 확인. 실제 배당 지급 기준과 재원. 확정된 투자와 현금 유출. 실적 숫자와 기존 전망의 차이.",
        "나중에: 같은 접수번호는 다시 안 보냄. 정정은 새 접수번호일 때만 따로 옴. 평일 19:00 한 번이라 그 이후 공시는 다음 평일에 봄. 실시간도 아니고 본문 숫자 비교도 아님.",
        "익절/줄일 때: 이 알림에는 가격과 비중과 매수 일정이 없음. 수량은 따로 둔 계획만 따름.",
        "하지 말 것: 제목만 보고 매매. 공시에 없는 기준일, 비중, 할인율을 만들어 적용. 조회 실패를 공시 없음으로 읽기.",
    ]
    return title, "\n".join(lines), 3, f"{code}:{row['id']}"


def ops_message(stamp: str, detail: str) -> tuple[str, str]:
    detail = show_title(detail)[:300] or "원인 미상"
    title = f"{TAG} 009540/329180 공시 조회 실패"
    lines = [
        stamp,
        "이게 뭐 알림: 009540, 329180 공식 공시 목록을 읽지 못한 안내. 매매 신호 아님.",
        f"지금 결과/상태: {detail}",
        "지금 할 일: KIND 또는 DART 에서 두 종목 공시를 직접 확인.",
        "나중에: 같은 날 안내는 한 번. 다음 평일 19:00 에 다시 읽음.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 안내를 공시 없음이나 매매 신호로 읽기.",
    ]
    return title, "\n".join(lines)


def _body_of(row: dict, body_get) -> str:
    if "body" in row and row["body"] is not None and str(row["body"]).strip():
        return str(row["body"])
    getter = body_get or default_body
    text = getter(row)
    if not str(text or "").strip():
        raise RuntimeError("본문 비어 있음")
    return str(text)


def process_ticker(co: dict, bucket: dict, rows: list[dict], stamp: str, body_get=None, since: str | None = None) -> tuple[list, list]:
    """since 가 주어지면 첫 실행에서도 since 이후 공시는 보낸다. since 이전 공시만 조용히 시드한다.
    since 가 없으면 예전 방식대로 첫 실행의 모든 해당 공시를 시드만 한다."""
    code = co["code"]
    name = co["name"]
    names = names_of(co)
    seen = set(bucket.get("seen") or [])
    keys = set(bucket.get("title_keys") or [])
    pending = set(bucket.get("seed_pending") or [])
    first = not bucket.get("seeded_at")
    alerts, errors = [], []
    for row in rows:
        did = f"{code}:{row['id']}"
        day = norm_when(row.get("filed") or "")[0]
        old = first and (since is None or not day or day < since)
        tkey = title_key(code, row)
        if did in seen or (tkey and tkey in keys):
            if did not in seen:
                seen.add(did)
            continue
        title = clean_title(row.get("title") or "", names)
        row = {**row, "title": title}
        decision = title_decision(title)
        if decision is None:
            continue
        tokens: list[str] = []
        if decision in {NEED, "해외투자문구"}:
            if decision == NEED:
                try:
                    body = scrub(_body_of(row, body_get), names)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{code} 본문 {row['id']}: {exc}")
                    if old:
                        pending.add(did)
                    continue
                tokens = token_labels(title + " " + body)
                decision = "해외투자문구" if tokens_ok(tokens) else None
            else:
                tokens = token_labels(scrub(title, names))
        if decision is None:
            seen.add(did)
            if tkey:
                keys.add(tkey)
            pending.discard(did)
            continue
        silent = old or did in pending
        seen.add(did)
        if tkey:
            keys.add(tkey)
        pending.discard(did)
        if silent:
            print(f"seed plan (not saved until auto): {did} {row.get('filed')} {decision} {show_title(title)[:60]}")
            continue
        alerts.append(build_message(stamp, code, name, row, decision, tokens if decision == "해외투자문구" else []))
    if first:
        bucket["seeded_at"] = stamp
        if since is None:
            print(f"seed plan {code}: seen {len(seen)}, pending {len(pending)}, no send this pass")
        else:
            print(f"first pass {code}: seen {len(seen)}, pending {len(pending)}, send {len(alerts)} (since {since}; older rows seeded only)")
    bucket["seen"] = sorted(seen)
    bucket["title_keys"] = sorted(keys)
    bucket["seed_pending"] = sorted(pending)
    if not alerts and not first:
        print(f"{code} 새 해당 공시 없음")
    return alerts, errors


def window_open(now: datetime, cfg: dict) -> bool:
    start = datetime.strptime(cfg["window_start"], "%Y-%m-%d").replace(tzinfo=KST)
    end = datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    return start <= now <= end


def run(mode: str, now: datetime, cfg: dict, pub=publish, state_path: Path | None = None, fetch=None, body_get=None) -> int:
    dry = mode == "dry"
    path = state_path or STATE_PATH
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    print(f"{stamp} {NAME} mode={mode}")
    if not window_open(now, cfg):
        print(f"감시 기간 밖({cfg['window_start']} ~ {cfg['ends_kst']}): 조용히 종료")
        return 0
    st = load_state(path, cfg)
    since, today = cfg["window_start"], now.strftime("%Y-%m-%d")
    alerts, errors = [], []
    for co in cfg["companies"]:
        code = co["code"]
        try:
            LAST_COUNTS.pop(code, None)
            rows, src = (fetch or fetch_company)(co, since, today)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{code} 목록 조회 실패: {exc}")
            print(f"{code} fetch failed (not seeded, retry next run): {exc}", file=sys.stderr)
            continue
        raw_n, win_n = LAST_COUNTS.get(code, (None, len(rows)))
        kind = "empty-ok" if raw_n == 0 else "ok"
        print(f"{code} source={src} raw={raw_n if raw_n is not None else '?'} window={win_n} ({since}~{today}) {kind}")
        got, errs = process_ticker(co, st["tickers"][code], rows, stamp, body_get=body_get, since=since)
        alerts.extend(got)
        errors.extend(f"{code} {e}" if not str(e).startswith(code) else e for e in errs)
    ops = None
    if errors and st.get("ops_day") != today:
        ops = ops_message(stamp, "; ".join(errors))
        st["ops_day"] = today
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, message, pri, key in alerts:
        check_text(title, message)
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)} DEDUP: {key}\n{message}\n")
    if ops:
        check_text(*ops)
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if dry:
        print("DRY RUN: no publish, state not written, not seeded")
        return 0
    for title, message, pri, key in alerts:
        pub(topic, title, message, pri, ["memo"], server, config_name=NAME, dedup_key=str(key))
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}", file=sys.stderr)
    st["updated_at"] = stamp
    save_state(path, st)
    return 0


def _co(code="009540", name="HD한국조선해양"):
    return {"code": code, "name": name, "aliases": ["에이치디한국조선해양", "한국조선해양", name]}


def _row(rid, title, filed="2026-11-20 16:00", body=None, src="KIND"):
    row = {
        "id": rid,
        "acptno": rid.split(":", 1)[-1] if rid.startswith("kind:") else None,
        "title": title,
        "filed": filed,
        "src": src,
        "url": "https://kind.krx.co.kr/common/disclsviewer.do?method=search&acptno=20261120000001",
    }
    if body is not None:
        row["body"] = body
    return row


def selftest() -> int:
    cfg = load_cfg()
    names = names_of(cfg["companies"][0])
    assert clean_title("에이치디한국조선해양(주) 현금ㆍ현물 배당 결정", names) == "현금ㆍ현물 배당 결정"
    cases = {
        "현금ㆍ현물 배당 결정": "현금배당",
        "(정정)현금ㆍ현물 배당 결정": "현금배당",
        "현금배당결정": "현금배당",
        "현금ㆍ현물배당을 위한 주주명부폐쇄(기준일) 결정": "배당기준일",
        "주주명부폐쇄기간 또는 기준일 설정": "기준일",
        "기업가치 제고 계획(자율공시)": "기업가치제고",
        "주주환원 정책(자율공시)": "주주환원",
        "배당정책(자율공시)": "주주환원",
        "연결재무제표기준영업(잠정)실적(공정공시)": "잠정실적",
        "영업(잠정)실적(공정공시)(자회사의 주요경영사항)": "잠정실적",
        "매출액또는손익구조30%(대규모법인은15%)이상변경": "손익구조",
        "영업실적 등에 대한 전망(공정공시)": "실적전망",
        "분기보고서 (2026.09)": "정기보고서",
        "[기재정정]사업보고서 (2025.12)": "정기보고서",
        "감사보고서 제출": "정기보고서",
        "반기보고서 (2026.06)": "정기보고서",
        "미국 조선소 합작 출자(자율공시)": "해외투자문구",
        "신규시설투자등": NEED,
        "타법인 주식 및 출자증권 취득결정": NEED,
        "단일판매ㆍ공급계약체결": None,
        "단일판매ㆍ공급계약체결(자회사의 주요경영사항)": None,
        "기업설명회(IR) 개최(안내공시)": None,
        "중간(분기)배당락 기준가격 안내": None,
        "풍문 또는 보도에 대한 해명(미확정)": None,
        "주식선물ㆍ주식옵션 2단계 가격제한폭 확대요건 도달(상승)": None,
        "미국 출자(자율공시)": None,
        "인도네시아 조선소 출자(자율공시)": None,
    }
    for title, expect in cases.items():
        got = title_decision(clean_title(title, names))
        assert got == expect, (title, got, expect)

    domestic = "신규 시설투자 등 1. 투자구분 신규시설투자 투자대상 대출력 발전엔진 생산능력 확충 투자금액(원) 833,600,000,000 투자목적 엔진 공장"
    us_body = "타법인 출자 투자대상 미국 버지니아 조선소 합작법인 지분 취득"
    india_body = "인도 조선소 합작 법인에 대한 출자 결정"
    indo_body = "인도네시아 조선소 지분 출자"
    delivery = "선박 인도 일정 변경. 투자금액 1원. 신규시설투자 국내 야드 아님 공장"
    calls = {"n": 0}

    def body_get(row):
        calls["n"] += 1
        if row.get("body"):
            return row["body"]
        raise RuntimeError("본문 조회 실패")

    co = _co()
    bucket = empty_bucket()
    old_div = _row("kind:20260729000001", "현금ㆍ현물 배당 결정", "2026-07-29 15:40")
    supply = _row("kind:20261106000002", "단일판매ㆍ공급계약체결", "2026-11-06 18:00", body=us_body)
    plant = _row("kind:20260910000003", "신규시설투자등", "2026-09-10 15:30", body=domestic)
    stamp = "2026-11-08 19:00 KST"
    alerts, errs = process_ticker(co, bucket, [old_div, supply, plant], stamp, body_get=body_get)
    assert alerts == [] and errs == [] and bucket["seeded_at"] == stamp, (alerts, errs, bucket)
    assert "009540:kind:20260729000001" in bucket["seen"]
    assert "009540:kind:20260910000003" in bucket["seen"]
    assert "009540:kind:20261106000002" not in bucket["seen"]
    assert calls["n"] == 0, calls  # 시드 본문은 행에 이미 있음. 공급계약은 본문을 열지 않음

    alerts, errs = process_ticker(co, bucket, [old_div, supply, plant], stamp, body_get=body_get)
    assert alerts == [] and errs == []

    new_div = _row("kind:20261120000011", "(정정)현금ㆍ현물 배당 결정", "2026-11-20 16:10")
    alerts, errs = process_ticker(co, bucket, [old_div, new_div], stamp, body_get=body_get)
    assert len(alerts) == 1 and errs == [], alerts
    title, message, pri, key = alerts[0]
    assert title.startswith(f"{TAG} 009540 공시 확인/재검토 | 현금배당"), title
    assert pri == 3 and key == "009540:kind:20261120000011"
    assert "실제 배당 지급 기준" in message and "확정된 투자와 현금 유출" in message and "기존 전망" in message
    assert "2026-11-20 16:10" in message and "KIND" in message and "kind.krx.co.kr" in message
    for banned in ("60:40", "50/30/20", "75%", "2027-02", "2027년 2월"):
        assert banned not in title and banned not in message
    check_text(title, message)
    alerts, _ = process_ticker(co, bucket, [new_div], stamp, body_get=body_get)
    assert alerts == []

    noise = _row("kind:20261121000012", "임원주요주주특정증권등소유상황보고서", "2026-11-21 09:00")
    alerts, errs = process_ticker(co, bucket, [noise, supply], stamp, body_get=body_get)
    assert alerts == [] and errs == []
    assert calls["n"] == 0

    us = _row("kind:20261201000021", "타법인 주식 및 출자증권 취득결정", "2026-12-01 15:10", body=us_body)
    alerts, errs = process_ticker(co, bucket, [us], stamp, body_get=body_get)
    assert len(alerts) == 1 and errs == []
    assert "해외투자문구" in alerts[0][0] and "009540" in alerts[0][0]
    assert "미국" in alerts[0][1] and "조선소" in alerts[0][1] and "단정하지 않음" in alerts[0][1]
    check_text(alerts[0][0], alerts[0][1])

    india = _row("kind:20261202000022", "신규시설투자등", "2026-12-02 15:10", body=india_body)
    alerts, _ = process_ticker(co, bucket, [india], stamp, body_get=body_get)
    assert len(alerts) == 1 and "인도조선" in alerts[0][1] and "조선소" in alerts[0][1]

    no_india = _row("kind:20261203000023", "신규시설투자등", "2026-12-03 15:10", body=indo_body)
    ship = _row("kind:20261204000024", "신규시설투자등", "2026-12-04 15:10", body=delivery)
    alerts, errs = process_ticker(co, bucket, [no_india, ship], stamp, body_get=body_get)
    assert alerts == [] and errs == []
    assert "009540:kind:20261203000023" in bucket["seen"]

    bare = _row("kind:20261205000025", "미국 출자(자율공시)", "2026-12-05 11:00", body=us_body)
    alerts, _ = process_ticker(co, bucket, [bare], stamp, body_get=body_get)
    assert alerts == [] and calls["n"] == 0

    vup = _row("kind:20260401000030", "기업가치 제고 계획(자율공시)", "2026-11-22 10:00", body="스마트 조선소와 미국 협력 언급")
    fresh = empty_bucket()
    fresh["seeded_at"] = stamp
    alerts, _ = process_ticker(co, fresh, [vup], stamp, body_get=body_get)
    assert len(alerts) == 1 and alerts[0][0].endswith("| 기업가치제고")
    assert "해외투자문구" not in alerts[0][0]

    kind_same = _row("kind:20270115000040", "현금ㆍ현물 배당 결정", "2027-01-15 16:00")
    nav_same = _row("naver:999", "현금ㆍ현물 배당 결정", "2027-01-15", src="네이버 KRX 공시 피드")
    b2 = empty_bucket()
    b2["seeded_at"] = stamp
    first, _ = process_ticker(co, b2, [kind_same], stamp, body_get=body_get)
    second, _ = process_ticker(co, b2, [nav_same], stamp, body_get=body_get)
    assert len(first) == 1 and second == []
    assert "009540:naver:999" in b2["seen"]

    broken = _row("kind:20270116000041", "신규시설투자등", "2027-01-16 15:00")
    b3 = empty_bucket()
    alerts, errs = process_ticker(co, b3, [broken, old_div], stamp, body_get=body_get)
    assert alerts == [] and errs and "본문" in errs[0]
    assert b3["seeded_at"] == stamp
    assert "009540:kind:20260729000001" in b3["seen"]
    assert "009540:kind:20270116000041" in b3["seed_pending"]
    assert "009540:kind:20270116000041" not in b3["seen"]
    later, errs2 = process_ticker(co, b3, [broken, _row("kind:20270117000042", "현금배당결정", "2027-01-17 16:00")], stamp, body_get=lambda r: us_body)
    assert errs2 == []
    assert len(later) == 1 and later[0][0].endswith("| 현금배당")
    assert "009540:kind:20270116000041" in b3["seen"]
    assert "해외투자" not in later[0][0]

    html = """
    <table><tr>
      <td>2026-11-08 16:02</td>
      <td><a onclick="openDisclsViewer('20261108000111','')" title='현금ㆍ현물 배당 결정'>현금ㆍ현물 배당 결정</a></td>
      <td>공시차트 주가차트</td>
    </tr></table>
    """
    parsed = parse_kind_html(html, co, "2026-11-08", "2026-11-08")
    assert len(parsed) == 1 and parsed[0]["id"] == "kind:20261108000111" and parsed[0]["title"] == "현금ㆍ현물 배당 결정"
    assert parse_kind_html(html, co, "2026-11-09", "2026-11-09") == []
    assert parse_kind_html("<html>조회된 결과값이 없습니다</html>", co, "2026-11-08", "2026-11-08") == []
    for bad in ("<html>blocked</html>", "<table><tr><td>점검 중</td></tr></table>"):
        try:
            parse_kind_html(bad, co, "2026-11-08", "2026-11-08")
            raise AssertionError("bad kind page should raise: " + bad)
        except RuntimeError as exc:
            assert "형식" in str(exc)
    real_empty = "<table><tbody><tr class='first'><td class='first null' colspan='6'>조회된 결과값이 없습니다.</td></tr></tbody></table>"
    assert parse_kind_html(real_empty, co, "2026-11-08", "2026-11-08") == [] and LAST_COUNTS["009540"] == (0, 0)
    parse_kind_html(html, co, "2026-11-09", "2026-11-09")
    assert LAST_COUNTS["009540"] == (1, 0)  # 원본 1행, 기간 필터 후 0행
    try:
        parse_naver_rows({"error": "blocked"}, co, "2026-11-08", "2026-11-08")
        raise AssertionError("bad naver payload should raise")
    except RuntimeError as exc:
        assert "네이버" in str(exc)

    # 첫 실행 + since: 시작일 이후 공시는 보내고, 이전 공시만 시드한다.
    b4 = empty_bucket()
    nov9 = _row("kind:20261109000051", "현금ㆍ현물 배당 결정", "2026-11-09 16:00")
    nov9_need = _row("kind:20261109000052", "신규시설투자등", "2026-11-09 16:30")
    alerts, errs = process_ticker(co, b4, [old_div, nov9, nov9_need], stamp, body_get=body_get, since="2026-11-08")
    assert len(alerts) == 1 and alerts[0][3] == "009540:kind:20261109000051", alerts
    assert errs and "본문" in errs[0]
    assert "009540:kind:20260729000001" in b4["seen"] and b4["seeded_at"] == stamp
    assert "009540:kind:20261109000052" not in b4["seed_pending"] and "009540:kind:20261109000052" not in b4["seen"]
    retry, _ = process_ticker(co, b4, [nov9_need], stamp, body_get=lambda r: us_body, since="2026-11-08")
    assert len(retry) == 1 and "해외투자문구" in retry[0][0]  # 본문 실패한 신규 공시는 다음 실행에서 보냄
    nav = parse_naver_rows(
        [{"no": "7", "datetime": "2026-11-08T18:01:00", "title": "에이치디한국조선해양(주) 분기보고서 (2026.09)", "contents": "<p>본문</p>"}],
        co, "2026-11-08", "2026-11-08",
    )
    assert len(nav) == 1 and nav[0]["id"] == "naver:7" and nav[0]["title"].startswith("분기보고서") and "본문" in nav[0]["body"]
    assert parse_naver_rows([{"no": "8", "datetime": "2026-11-07T18:01:00", "title": "분기보고서"}], co, "2026-11-08", "2026-11-08") == []

    saved = {"k": 0, "n": 0}

    def fake_kind_empty(c, s, u):
        saved["k"] += 1
        return []

    def fake_nav_down(c, s, u):
        saved["n"] += 1
        raise RuntimeError("down")

    def fake_kind_down(c, s, u):
        saved["k"] += 1
        raise RuntimeError("403")

    def fake_nav_empty(c, s, u):
        saved["n"] += 1
        return []

    orig_k, orig_n = kind_list, naver_list
    try:
        globals()["kind_list"] = fake_kind_empty
        globals()["naver_list"] = fake_nav_down
        rows, src = fetch_company(co, "2026-11-08", "2026-11-08")
        assert rows == [] and src == "KIND" and saved["n"] == 0
        globals()["kind_list"] = fake_kind_down
        globals()["naver_list"] = fake_nav_empty
        rows, src = fetch_company(co, "2026-11-08", "2026-11-08")
        assert rows == [] and src == "네이버"
        globals()["naver_list"] = fake_nav_down
        try:
            fetch_company(co, "2026-11-08", "2026-11-08")
            raise AssertionError("both sources down must raise")
        except RuntimeError as exc:
            msg = str(exc)
            assert "403" in msg and "down" in msg
            assert "0건" not in msg
    finally:
        globals()["kind_list"] = orig_k
        globals()["naver_list"] = orig_n

    now = datetime(2026, 11, 20, 19, 0, tzinfo=KST)
    late = datetime(2027, 10, 1, 19, 0, tzinfo=KST)
    assert run("auto", late, cfg, pub=lambda *a, **k: (_ for _ in ()).throw(AssertionError("pub")), fetch=lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetch"))) == 0

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "hdship.json"
        save_state(path, fresh_state(cfg))
        before = path.read_bytes()
        sent = []

        def pub(*a, **k):
            sent.append((a, k))

        def listing(c, s, u):
            if c["code"] == "009540":
                raise RuntimeError("timeout")
            return [_row("kind:20261120000077", "현금배당결정", "2026-11-20 16:00")], "KIND"

        assert run("dry", now, cfg, pub=pub, state_path=path, fetch=listing, body_get=body_get) == 0
        assert sent == [] and path.read_bytes() == before

        assert run("auto", now, cfg, pub=pub, state_path=path, fetch=listing, body_get=body_get) == 0
        # 첫 실행이어도 window_start 이후 공시(329180 11/20 배당)는 보내고, 009540 조회 실패는 ops 로 알린다.
        assert len(sent) == 2, sent
        assert sent[0][0][0] == "cwally-port" and "329180" in sent[0][0][1] and sent[0][0][1].endswith("| 현금배당")
        sent.pop(0)
        assert sent[0][0][0] == "cwally-ops"
        assert "조회 실패" in sent[0][0][1] and "009540/329180" in sent[0][0][1]
        saved_st = json.loads(path.read_text(encoding="utf-8"))
        assert saved_st["ops_day"] == "2026-11-20"
        assert saved_st["tickers"]["009540"]["seeded_at"] is None
        assert saved_st["tickers"]["329180"]["seeded_at"]
        assert sent and "009540:kind:20261120000077" not in (saved_st["tickers"]["329180"]["seen"])
        assert any(x.endswith("20261120000077") for x in saved_st["tickers"]["329180"]["seen"])
        sent.clear()
        assert run("auto", now, cfg, pub=pub, state_path=path, fetch=listing, body_get=body_get) == 0
        assert sent == [], sent

        path2 = Path(td) / "seed.json"
        save_state(path2, fresh_state(cfg))
        before2 = path2.read_bytes()

        def both(c, s, u):
            return [old_div], "KIND"

        assert run("dry", now, cfg, pub=pub, state_path=path2, fetch=both, body_get=body_get) == 0
        assert sent == [] and path2.read_bytes() == before2
        assert run("auto", now, cfg, pub=pub, state_path=path2, fetch=both, body_get=body_get) == 0
        assert sent == []
        seeded = json.loads(path2.read_text(encoding="utf-8"))
        assert seeded["tickers"]["009540"]["seeded_at"]
        assert "009540:kind:20260729000001" in seeded["tickers"]["009540"]["seen"]

    ot, om = ops_message("T", "009540 목록 조회 실패: timeout")
    check_text(ot, om)
    assert route_topic("cwally-port", ot) == "cwally-ops"
    assert route_topic("cwally-port", title) == "cwally-port"
    print("selftest ok")
    return 0


class _Prefix:
    """selftest 출력 줄마다 [selftest] 를 붙인다. 실패 로그는 그대로 보이게 둔다."""

    def __init__(self, inner, tag: str = "[selftest] "):
        self.inner, self.tag, self.bol = inner, tag, True

    def write(self, s: str) -> int:
        for part in s.splitlines(keepends=True):
            if self.bol:
                self.inner.write(self.tag)
            self.inner.write(part)
            self.bol = part.endswith("\n")
        return len(s)

    def flush(self) -> None:
        self.inner.flush()


def run_selftest() -> int:
    import traceback
    out, err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Prefix(out), _Prefix(err)
    try:
        rc = selftest()
        print("note: [selftest] 403/timeout 줄은 테스트가 일부러 만든 가짜 장애, 실제 조회 아님")
        return rc
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        print("selftest FAILED", file=sys.stderr)
        return 1
    finally:
        sys.stdout.flush(); sys.stderr.flush()
        sys.stdout, sys.stderr = out, err


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return run_selftest()
    if mode not in {"auto", "dry"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|selftest>")
        return 2
    cfg = load_cfg()
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            stamp = now.strftime("%Y-%m-%d %H:%M KST")
            t, m = ops_message(stamp, f"스크립트 오류 {type(exc).__name__}: {exc}")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
