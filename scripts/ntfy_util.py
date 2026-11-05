#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ALLOWED = {"cwally-port", "cwally-ops"}
KIND_DEFAULT = {"port": "cwally-port", "flow": "cwally-port", "ops": "cwally-ops"}


def resolve_topic(kind: str = "port", configured: str | None = None) -> str:
    if kind == "ops":
        return "cwally-ops"
    return "cwally-port"


# 조회/수집/파싱 실패 안내는 매매 신호가 아니므로 cwally-ops로 보낸다 (AGENTS.md "토픽").
OPS_TITLE_RE = re.compile(r"조회\s?실패|수집\s?실패|파싱\s?실패|해석\s?불가|수급 누락|확인 불가|숫자 미확인|미게시")


def route_topic(topic: str, title: str | None) -> str:
    if title and OPS_TITLE_RE.search(title):
        return "cwally-ops"
    return topic if topic in ALLOWED else "cwally-port"


# 알림 종료 시점 안내 (AGENTS.md "Alert end line", 2026-10-07 승인).
# configs/<name>.json 의 ends_kst ("YYYY-MM-DD HH:MM" KST 또는 null)와 ends_note 를 읽어
# 본문 맨 끝에 "꺼지는 때: ..." 한 줄을 붙인다. name 은 실행 중인 스크립트 파일명
# (<name>_monitor.py)에서 얻는다. 칸이 없거나 읽기에 실패하면 아무것도 붙이지 않는다(발송은 계속).
CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs"
ENDS_PREFIX = "꺼지는 때:"
_NAME_SUFFIXES = ("_monitor", "_impl", "_fund")


def alert_name() -> str | None:
    env = os.environ.get("CWALLY_ALERT_NAME")
    if env:
        return env.strip()
    stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    for suf in _NAME_SUFFIXES:
        if stem.endswith(suf) and len(stem) > len(suf):
            return stem[: -len(suf)]
    return None


def ends_line(name: str | None = None) -> str | None:
    try:
        name = name or alert_name()
        if not name:
            return None
        cfg = json.loads((CONFIGS_DIR / f"{name}.json").read_text(encoding="utf-8"))
        if not isinstance(cfg, dict) or "ends_kst" not in cfg:
            return None
        end = cfg.get("ends_kst")
        note = str(cfg.get("ends_note") or "").strip()
        head = f"{str(end).strip()} KST" if end else "종료일 없음"
        return f"{ENDS_PREFIX} {head}" + (f" ({note})" if note else "")
    except Exception as exc:  # 종료 안내 실패가 알림 발송을 막으면 안 된다
        print(f"ends_line skipped: {exc}", file=sys.stderr)
        return None


def append_ends(message: str, name: str | None = None) -> str:
    if ENDS_PREFIX in (message or ""):
        return message
    line = ends_line(name)
    if not line:
        return message
    return f"{message.rstrip()}\n{line}" if message else line


def resolve_server(configured: str | None = None) -> str:
    return configured or os.environ.get("NTFY_SERVER") or "https://ntfy.sh"


# ---------------------------------------------------------------------------------------------
# ends_kst 발송 차단 (approved in chat 2026-10-20 12:01 KST: "ntfy_util.publish가 ends_kst를 확인하게 고쳐").
# cwally-dashboard "알림 끄기"는 configs/<name>.json 의 ends_kst 를 지금으로 바꾼다. 스크립트 대부분은
# ends_kst 를 직접 보지 않으므로 publish() 가 마지막 관문에서 확인한다.
#
# 어느 config 인가 (위에서부터 처음 찾은 것):
#   1. publish(..., config_name="<name>") 또는 config=<dict>  (선택 인자, 기존 호출은 안 바꿔도 됨)
#   2. 실행 중인 스크립트: CWALLY_ALERT_NAME env, sys.argv[0], __main__.__file__ 의
#      scripts/<name>_monitor.py (_impl/_fund 포함) -> configs/<name>.json
#      (burry_monitor -> runpy burry_impl, oneshot_pick -> subprocess 는 모두 이 단계에서 풀림)
#   3. 제목 맨 앞 [PREFIX] 와 configs 의 title_prefix (없으면 [NAME 대문자]) 매칭.
#      여러 config 가 같은 prefix 를 쓰면 전부 끝났을 때만 막는다.
#   못 찾으면 예전처럼 보낸다 (fail open, stderr 로그).
#
# 막는 규칙:
#   - ends_kst null/없음 -> 보냄. "YYYY-MM-DD HH:MM"(" KST" 꼬리, 초, T, / 허용) -> 그 시각 + 유예
#     (기본 59분 = oneshot_pick 60분 창, 기존 스크립트 자체 검사와 같음; config "ends_grace_min" 로 조정)
#     를 넘으면 막음. 날짜만 있으면 그날 KST 24:00 이후 막음. 해석 불가 -> stderr 경고 후 보냄.
#   - 항상 보냄: 제목이 [OPS] 로 시작, 워크플로 실패 안내, 그리고 cwally-ops 로 가는 안내 중 제목이
#     끝난 그 알림 자신의 prefix 로 시작하지 않는 것. 끝난 알림 자신의 조회 실패/데이터 누락 안내
#     ([NAME] ... 조회 실패 -> cwally-ops)는 같이 막는다 (꺼진 알림의 데이터 누락은 할 일이 없음).
#   - NTFY_IGNORE_ENDS=1 이면 검사 안 함 (비상용).
# 막으면 stdout 에 "ntfy skip: <name> ended <ends_kst> KST" 를 찍고 None 을 돌려준다 (보낼 때와 같음).
# 마지막 결과는 LAST_PUBLISH 에 남는다 ({"sent": bool, "reason": str, "name": ...}).
# ---------------------------------------------------------------------------------------------
KST = timezone(timedelta(hours=9))
DEFAULT_ENDS_GRACE_MIN = 59
OPS_TOPIC = "cwally-ops"
ALWAYS_SEND_TITLE_RE = re.compile(r"^\s*\[OPS\]|워크플로\s?(실패|검사 실패)|설치 확인|테스트")  # 설치 확인/테스트: 같은 날 재발송 허용 (pt15u)
_PREFIX_RE = re.compile(r"^\s*(\[[^\]]+\])")
LAST_PUBLISH: dict = {}

# selftest 안전장치 (2026-11-05, t102u): 'selftest' 인자로 실행됐는데 실행 스크립트(argv[0] 또는 __main__ 파일,
# runpy 래퍼면 실제 impl) 소스에 'selftest' 처리가 없으면, 이 모듈을 import 하는 순간 발송과 저장 전에 멈춘다 (exit 2).
# 자체 selftest 모드가 있는 스크립트는 소스에 'selftest' 문자열이 있으므로 그대로 동작한다.
SELFTEST_STOP_MSG = "selftest 모드 없음: 발송/저장 중단"


def _script_has_selftest() -> bool:
    cands = [sys.argv[0] if sys.argv else None, getattr(sys.modules.get("__main__"), "__file__", None)]
    for c in cands:
        try:
            if c and Path(c).is_file() and "selftest" in Path(c).read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            continue
    return False


def selftest_blocked() -> bool:
    """True when argv asks for selftest but the running script has no selftest mode of its own."""
    if "selftest" not in [a.lower() for a in sys.argv[1:]]:
        return False
    if os.environ.get("CWALLY_SELFTEST_OK") == "1":
        return False
    return not _script_has_selftest()


def _selftest_guard() -> None:
    if selftest_blocked():
        print(f"{SELFTEST_STOP_MSG} ({Path(sys.argv[0]).name if sys.argv else '?'})", file=sys.stderr)
        raise SystemExit(2)


_selftest_guard()


def _now_kst() -> datetime:
    return datetime.now(KST)


def parse_ends_kst(raw) -> tuple[datetime | None, bool]:
    """Return (cutoff_base, date_only). None = no end. Raises ValueError when unparseable."""
    if raw is None:
        return None, False
    if not isinstance(raw, str):
        raise ValueError(f"ends_kst not a string: {raw!r}")
    text = raw.strip()
    if not text or text.lower() in {"null", "none", "없음", "종료일 없음"}:
        return None, False
    text = re.sub(r"\s*\(?KST\)?$", "", text, flags=re.I).strip().replace("T", " ").replace("/", "-")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=KST), True
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST), False
        except ValueError:
            pass
    dt = datetime.fromisoformat(text)  # e.g. "2026-11-18 12:00+09:00"; ValueError if not
    return (dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)), False


def _load_config(name: str) -> dict | None:
    path = CONFIGS_DIR / f"{name}.json"
    if not path.is_file():
        return None
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return cfg if isinstance(cfg, dict) else None


def _stem_name(path: str | None) -> str | None:
    stem = Path(path).stem if path else ""
    for suf in _NAME_SUFFIXES:
        if stem.endswith(suf) and len(stem) > len(suf):
            return stem[: -len(suf)]
    return None


def _script_names() -> list[str]:
    out: list[str] = []
    env = (os.environ.get("CWALLY_ALERT_NAME") or "").strip()
    if env:
        out.append(env)
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    for cand in (sys.argv[0] if sys.argv else None, main_file):
        n = _stem_name(cand)
        if n and n not in out:
            out.append(n)
    return out


def _config_prefixes(name: str, cfg: dict) -> list[str]:
    raw = cfg.get("title_prefix")
    found = re.findall(r"\[[^\]]+\]", raw) if isinstance(raw, str) else []
    return found or [f"[{name.upper()}]"]


def resolve_alert_configs(title: str | None = None, config_name: str | None = None, config: dict | None = None) -> tuple[str, list[tuple[str, dict]]]:
    """(how, [(name, cfg), ...]); empty list = not resolved."""
    if isinstance(config, dict):
        return "param", [(str(config_name or config.get("name") or "?"), config)]
    if config_name:
        cfg = _load_config(config_name)
        if cfg is not None:
            return "param", [(config_name, cfg)]
        print(f"ntfy ends: config_name={config_name!r} not found; trying script name", file=sys.stderr)
    for n in _script_names():
        cfg = _load_config(n)
        if cfg is not None:
            return "script", [(n, cfg)]
    m = _PREFIX_RE.match(title or "")
    if m:
        want = m.group(1).upper()
        hits = []
        for path in sorted(CONFIGS_DIR.glob("*.json")):
            try:
                cfg = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(cfg, dict) and "ends_kst" in cfg and want in (p.upper() for p in _config_prefixes(path.stem, cfg)):
                hits.append((path.stem, cfg))
        if hits:
            return "prefix", hits
    return "none", []


def _ended(name: str, cfg: dict, now: datetime) -> tuple[bool, str]:
    raw = cfg.get("ends_kst")
    try:
        base, date_only = parse_ends_kst(raw)
    except Exception as exc:
        print(f"ntfy ends warning: {name} ends_kst {raw!r} unparseable ({exc}); sending", file=sys.stderr)
        return False, "unparseable"
    if base is None:
        return False, "no end"
    if date_only:
        cutoff = base + timedelta(days=1)
        return now >= cutoff, str(raw)
    try:
        grace = int(cfg.get("ends_grace_min", DEFAULT_ENDS_GRACE_MIN))
    except (TypeError, ValueError):
        grace = DEFAULT_ENDS_GRACE_MIN
    return now > base + timedelta(minutes=max(grace, 0)), str(raw)


def ends_skip_reason(topic: str, title: str | None, config_name: str | None = None, config: dict | None = None, now: datetime | None = None) -> str | None:
    """Return a skip line when this publish must not go out because the alert ended, else None."""
    try:
        if (os.environ.get("NTFY_IGNORE_ENDS") or "").strip().lower() in {"1", "true", "yes", "on"}:
            return None
        if title and ALWAYS_SEND_TITLE_RE.search(title):
            return None
        how, cands = resolve_alert_configs(title, config_name, config)
        if not cands:
            print(f"ntfy ends: no config for title {title!r}; sending", file=sys.stderr)
            return None
        now = now or _now_kst()
        results = [(n, c) + _ended(n, c, now) for n, c in cands]
        if not all(r[2] for r in results):
            return None
        if topic == OPS_TOPIC:
            own = [p.upper() for n, c, _, _ in results for p in _config_prefixes(n, c)]
            m = _PREFIX_RE.match(title or "")
            if not m or m.group(1).upper() not in own:
                return None  # ops notice that is not this ended alert's own -> always send
        names = ",".join(r[0] for r in results)
        ends = ",".join(r[3] for r in results)
        return f"ntfy skip: {names} ended {ends} KST (via {how}; title={title!r})"
    except Exception as exc:  # 검사 실패가 발송을 막으면 안 된다
        print(f"ntfy ends check failed ({exc}); sending", file=sys.stderr)
        return None


# 하루 한 번 발송 보호 (dup-guard, 2026-11-04 pt13u; 슬롯 범위 2026-11-05 pt15u).
# state 저장(git push)이 실패해도 늦은 백업 실행이 같은 알림을 다시 보내지 않게, cwally-port 로 보내기 직전에
# since 이후 같은 topic 에 제목이 완전히 같은 메시지가 이미 있으면 보내지 않는다.
# since: 환경변수 DUP_GUARD_SINCE (unix, Decide jobs 가 슬롯 시작 10분 전으로 넣음). 없으면 오늘 00:00 KST.
#   morning 08:50, afternoon 15:10, 0090b0/flow/trend 15:30, k203780 18:50, portlight 20:50 KST.
#   tsla/grab: 뉴욕 정규장 시작(서머타임 22:30 KST, 표준시 23:30 KST). 원샷: 예정 시각 - 10분.
# 확인 출처: 1) ntfy poll  2) state/archive.json (ntfy.sh 캐시 12시간보다 오래된 발송 대비)
# poll 실패는 fail-open. 끄기: NTFY_DUP_GUARD=0. cwally-ops 와 ALWAYS_SEND_TITLE_RE(설치 확인/테스트 포함)는 보호하지 않는다.
DUP_GUARD_TOPICS = {"cwally-port"}
ARCHIVE_PATH = Path(__file__).resolve().parents[1] / "state" / "archive.json"
DUP_GUARD_LOG: list[str] = []
# 슬롯 시작(KST HH, MM). since = 시작 - 10분.
DUP_GUARD_SLOT_START_KST = {
    "morning": (9, 0),
    "afternoon": (15, 20),
    "0090b0": (15, 40),
    "flow": (15, 40),
    "trend": (15, 40),
    "k203780": (19, 0),
    "portlight": (21, 0),
}
DUP_GUARD_LEAD_MIN = 10


def _kst_midnight_unix(now: datetime | None = None) -> int:
    now = (now or _now_kst()).astimezone(KST)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _us_eastern_dst(d: datetime) -> bool:
    """US DST (2nd Sun Mar ~ 1st Sun Nov). d is a calendar date in Eastern terms."""
    y = d.year
    # 2nd Sunday of March
    mar1 = datetime(y, 3, 1, tzinfo=timezone.utc)
    dst_start = mar1.replace(day=1 + (6 - mar1.weekday()) % 7 + 7)  # first Sun + 7
    # 1st Sunday of November
    nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_end = nov1.replace(day=1 + (6 - nov1.weekday()) % 7)
    day = d.date() if hasattr(d, "date") else d
    return dst_start.date() <= day < dst_end.date()


def ny_session_open_unix(now: datetime | None = None) -> int:
    """Unix of today's New York regular-session open, expressed in KST wall clock (22:30 DST / 23:30 EST).
    If now is before that open on the KST calendar day, use yesterday's open (overnight session)."""
    now = (now or _now_kst()).astimezone(KST)
    # NY date roughly: before 06:00 KST still belongs to previous NY day for open lookup
    ny_approx = now - timedelta(hours=13)  # EST offset; DST handled below via KST open hour
    # Prefer KST wall-clock of open for the NY trading day that contains `now`
    # Session open in NY = 09:30 ET -> KST 22:30 (EDT) or 23:30 (EST)
    # Find which NY calendar day is in session: if KST hour >= open_hour yesterday, or before ~06:30 next day
    def open_for_ny_date(ny_day):
        dst = _us_eastern_dst(datetime(ny_day.year, ny_day.month, ny_day.day, tzinfo=timezone.utc))
        hh, mm = (22, 30) if dst else (23, 30)
        # open is on KST calendar day = ny_day (EDT: NY Mon 09:30 = KST Mon 22:30; EST: = KST Tue 23:30... wait)
        # Actually: NY Mon 09:30 EDT = UTC 13:30 = KST Mon 22:30. Same calendar date.
        # NY Mon 09:30 EST = UTC 14:30 = KST Mon 23:30. Same calendar date.
        return datetime(ny_day.year, ny_day.month, ny_day.day, hh, mm, tzinfo=KST)

    # NY session day: if now KST is before today's open, the active session (if any) is yesterday's
    today_open = open_for_ny_date(now.date())
    if now < today_open:
        # overnight: still in previous NY session if within ~7h after open (until ~05:30/06:30)
        yday = (now - timedelta(days=1)).date()
        yopen = open_for_ny_date(yday)
        if now >= yopen:
            return int(yopen.timestamp())
        return int(today_open.timestamp())  # before any session today; use today's upcoming open as since
    return int(today_open.timestamp())


def dup_guard_since_unix(job: str | None = None, schedule: str | None = None, now: datetime | None = None) -> int:
    """Unix since for DUP_GUARD_SINCE. job = slot name; schedule = GitHub cron (UTC 'M H D mon *') for oneshots."""
    now = (now or _now_kst()).astimezone(KST)
    job = (job or "").strip()
    if job in {"tsla", "grab"}:
        return ny_session_open_unix(now)
    if job in DUP_GUARD_SLOT_START_KST:
        hh, mm = DUP_GUARD_SLOT_START_KST[job]
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # overnight backup after midnight (before 06:00 KST) for evening slots still belongs to yesterday
        if job in {"k203780", "portlight"} and now.hour < 6 and start > now:
            start -= timedelta(days=1)
        elif start > now + timedelta(hours=12):
            start -= timedelta(days=1)
        return int((start - timedelta(minutes=DUP_GUARD_LEAD_MIN)).timestamp())
    sched = (schedule or "").strip()
    if sched and sched.count(" ") >= 3:
        try:
            parts = sched.split()
            mi, ho, dom, mon = int(parts[0]), int(parts[1]), parts[2], parts[3]
            if dom.isdigit() and mon.isdigit():
                utc = datetime(now.year, int(mon), int(dom), ho, mi, tzinfo=timezone.utc)
                # if UTC date is in the future relative to now by >180d, try previous year
                if utc - now > timedelta(days=180):
                    utc = utc.replace(year=now.year - 1)
                kst = utc.astimezone(KST)
                return int((kst - timedelta(minutes=DUP_GUARD_LEAD_MIN)).timestamp())
        except (TypeError, ValueError, OverflowError):
            pass
    return _kst_midnight_unix(now)


def resolve_dup_guard_since(now: datetime | None = None) -> int:
    raw = (os.environ.get("DUP_GUARD_SINCE") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            _dup_log(f"dup-guard DUP_GUARD_SINCE={raw!r} not int; falling back to midnight KST")
    return _kst_midnight_unix(now)


def _dup_poll(server: str, topic: str, since: int) -> list[dict]:
    url = f"{server.rstrip('/')}/{topic}/json?poll=1&since={since}"
    req = urllib.request.Request(url, headers={"User-Agent": "cwally-alerts-dup-guard"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("event", "message") == "message":
            out.append(msg)
    return out


def _dup_archive(topic: str, since: int, path: Path | None = None) -> list[dict]:
    try:
        data = json.loads((path or ARCHIVE_PATH).read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else data
    out = []
    for m in items or []:
        try:
            if m.get("topic") == topic and int(m.get("time") or 0) >= since:
                out.append(m)
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _dup_log(line: str) -> None:
    DUP_GUARD_LOG.append(line)
    print(line)


def dup_guard_skip(topic: str, title: str | None, server: str | None = None, *, now: datetime | None = None,
                   poll=None, archive=None, dedup_key: str | None = None) -> bool:
    """True = same title (or dedup_key tag) already published to this topic since DUP_GUARD_SINCE; caller must not send."""
    if (os.environ.get("NTFY_DUP_GUARD") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if topic not in DUP_GUARD_TOPICS or not title:
        return False
    if ALWAYS_SEND_TITLE_RE.search(title):
        return False
    since = resolve_dup_guard_since(now)
    poll = poll or _dup_poll
    archive = archive or _dup_archive
    key_tag = dedup_key_tag(dedup_key) if dedup_key else None

    def _hit(msgs: list[dict]) -> str | None:
        for m in msgs:
            if key_tag and key_tag in _msg_tags(m):
                return f"dedup_key tag {key_tag}"
            if m.get("title") == title:
                return "same title"
        return None

    try:
        reason = _hit(poll(server or resolve_server(), topic, since))
        if reason:
            _dup_log(f"dup-guard skip: {topic} {reason} already sent since {since} (ntfy poll): {title}")
            return True
    except Exception as exc:  # fail-open
        _dup_log(f"dup-guard poll failed ({type(exc).__name__}: {exc}); fail-open, archive check then send")
    try:
        reason = _hit(archive(topic, since))
        if reason:
            _dup_log(f"dup-guard skip: {topic} {reason} already sent since {since} (state/archive.json): {title}")
            return True
    except Exception as exc:
        _dup_log(f"dup-guard archive check failed ({type(exc).__name__}); send")
    return False


def dedup_key_tag(key: str | None) -> str | None:
    """Stable short tag for a disclosure receipt / id. dk-<10 hex of sha1>."""
    if not key:
        return None
    import hashlib
    return "dk-" + hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:10]


def _msg_tags(m: dict) -> set[str]:
    t = m.get("tags")
    if isinstance(t, list):
        return {str(x) for x in t}
    if isinstance(t, str):
        return {x for x in t.replace(",", " ").split() if x}
    return set()


def dup_guard_skip_payload(server: str | None, payload: bytes) -> bool:
    """For scripts that build their own ntfy JSON payload (topic/title keys) and POST it directly."""
    try:
        obj = json.loads(payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload)
    except Exception:
        return False
    return dup_guard_skip(str(obj.get("topic") or ""), obj.get("title"), server)


def publish(
    topic: str,
    title: str,
    message: str,
    priority: int = 3,
    tags: list[str] | None = None,
    server: str | None = None,
    *,
    config_name: str | None = None,
    config: dict | None = None,
    append_end: bool = True,
) -> None:
    if selftest_blocked():  # lazy import 뒤 호출 대비, import 시점 가드와 같은 조건
        print(f"{SELFTEST_STOP_MSG}: publish refused", file=sys.stderr)
        LAST_PUBLISH.clear()
        LAST_PUBLISH.update({"sent": False, "reason": "selftest_guard", "title": title})
        raise SystemExit(2)
    topic = route_topic(topic, title)
    skip = ends_skip_reason(topic, title, config_name=config_name, config=config)
    if skip:
        print(skip)
        LAST_PUBLISH.clear()
        LAST_PUBLISH.update({"sent": False, "reason": skip, "title": title})
        return None
    if dup_guard_skip(topic, title, server):
        LAST_PUBLISH.clear()
        LAST_PUBLISH.update({"sent": True, "dup": True, "reason": "dup-guard: already published today", "title": title})
        return None
    ends_name = config_name if config_name and config is None and (CONFIGS_DIR / f"{config_name}.json").is_file() else None
    if append_end:  # False only for legacy callers whose body never had the end line (hce)
        message = append_ends(message, ends_name)
    payload = json.dumps(
        {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags or ["chart_with_upwards_trend"],
        }
    ).encode()
    req = urllib.request.Request(
        (server or resolve_server()).rstrip("/"),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())
    LAST_PUBLISH.clear()
    LAST_PUBLISH.update({"sent": True, "reason": "", "title": title})
    return None


def record_fired(st: dict, day: str, key: str, priority: int, title: str, now: datetime | None = None, cap: int = 60) -> bool:
    """Append one sent-history entry to st["fired"] only if the last publish() really sent.

    Schema per entry: {date (judged data date), key (condition key, joined with + for a combined message),
    priority, title, sent_at (ISO)}. Keeps the last `cap` entries. Returns True when recorded.
    A skipped send (ends_kst passed, LAST_PUBLISH sent=False) or no publish yet records nothing.
    """
    if not LAST_PUBLISH.get("sent"):
        return False
    when = now or datetime.now(timezone(timedelta(hours=9)))
    lst = list(st.get("fired") or [])
    lst.append({"date": day, "key": key, "priority": priority, "title": title, "sent_at": when.isoformat()})
    st["fired"] = lst[-cap:] if cap > 0 else []
    return True


def _dup_guard_selftest(check) -> int:
    """dup-guard checks with fake poll/archive; nothing touches the network."""
    from unittest import mock
    before = 0
    now = datetime(2026, 11, 4, 22, 30, tzinfo=KST)
    mid = _kst_midnight_unix(now)
    assert mid == int(datetime(2026, 11, 4, 0, 0, tzinfo=KST).timestamp())
    today = [{"event": "message", "time": mid + 19 * 3600, "title": "[203780] 11/04 일일 요약 | 특이사항 없음, 매매 신호 아님"}]
    seen_since = []

    def poll_ok(server, topic, since):
        seen_since.append(since)
        return [m for m in today if m["time"] >= since]

    def poll_bad(server, topic, since):
        raise OSError("unresolvable")

    no_arch = lambda topic, since: []  # noqa: E731
    env = {"NTFY_DUP_GUARD": "1"}
    with mock.patch.dict(os.environ, env, clear=False):
        os.environ.pop("DUP_GUARD_SINCE", None)
        t = today[0]["title"]
        check("dup-guard same title today -> skip", dup_guard_skip("cwally-port", t, "x", now=now, poll=poll_ok, archive=no_arch), True)
        check("dup-guard poll since = today 00:00 KST (unset)", seen_since[-1] == mid, True)
        check("dup-guard different title -> send", dup_guard_skip("cwally-port", t + " 2", "x", now=now, poll=poll_ok, archive=no_arch), False)
        nxt = datetime(2026, 11, 5, 19, 0, tzinfo=KST)
        check("dup-guard yesterday's title -> send", dup_guard_skip("cwally-port", t, "x", now=nxt, poll=poll_ok, archive=no_arch), False)
        check("dup-guard poll failure -> fail-open send", dup_guard_skip("cwally-port", t, "x", now=now, poll=poll_bad, archive=no_arch), False)
        check("dup-guard poll failure logged", any("fail-open" in x for x in DUP_GUARD_LOG[-2:]), True)
        arch = lambda topic, since: [m for m in today if m["time"] >= since]  # noqa: E731
        check("dup-guard poll failure + archive has title -> skip", dup_guard_skip("cwally-port", t, "x", now=now, poll=poll_bad, archive=arch), True)
        check("dup-guard ops topic never guarded", dup_guard_skip("cwally-ops", t, "x", now=now, poll=poll_ok, archive=no_arch), False)
        with mock.patch.dict(os.environ, {"NTFY_DUP_GUARD": "0"}):
            check("dup-guard NTFY_DUP_GUARD=0 off", dup_guard_skip("cwally-port", t, "x", now=now, poll=poll_ok, archive=no_arch), False)
        # slot-scoped since (pt15u)
        afternoon_since = dup_guard_since_unix("afternoon", now=datetime(2026, 11, 5, 15, 25, tzinfo=KST))
        want_1510 = int(datetime(2026, 11, 5, 15, 10, tzinfo=KST).timestamp())
        check("dup-guard afternoon since = 15:10 KST", afternoon_since, want_1510)
        check("dup-guard morning since = 08:50 KST", dup_guard_since_unix("morning", now=datetime(2026, 11, 5, 9, 5, tzinfo=KST)), int(datetime(2026, 11, 5, 8, 50, tzinfo=KST).timestamp()))
        check("dup-guard 0090b0 since = 15:30 KST", dup_guard_since_unix("0090b0", now=datetime(2026, 11, 5, 15, 45, tzinfo=KST)), int(datetime(2026, 11, 5, 15, 30, tzinfo=KST).timestamp()))
        check("dup-guard k203780 since = 18:50 KST", dup_guard_since_unix("k203780", now=datetime(2026, 11, 5, 19, 5, tzinfo=KST)), int(datetime(2026, 11, 5, 18, 50, tzinfo=KST).timestamp()))
        # Nov 5 2026 is after US DST end (Nov 1) -> EST -> 23:30 KST
        check("dup-guard tsla since = 23:30 KST (EST)", dup_guard_since_unix("tsla", now=datetime(2026, 11, 5, 23, 45, tzinfo=KST)), int(datetime(2026, 11, 5, 23, 30, tzinfo=KST).timestamp()))
        # Jul 15 2026 is DST -> 22:30 KST
        check("dup-guard grab since = 22:30 KST (EDT)", dup_guard_since_unix("grab", now=datetime(2026, 7, 15, 23, 0, tzinfo=KST)), int(datetime(2026, 7, 15, 22, 30, tzinfo=KST).timestamp()))
        # oneshot: 10 7 28 10 * = 2026-10-28 07:10 UTC = 16:10 KST -> since 16:00
        check("dup-guard oneshot schedule -10min", dup_guard_since_unix("lgiq3", "10 7 28 10 *", now=datetime(2026, 10, 28, 16, 20, tzinfo=KST)), int(datetime(2026, 10, 28, 16, 0, tzinfo=KST).timestamp()))
        # DUP_GUARD_SINCE env wins
        with mock.patch.dict(os.environ, {"DUP_GUARD_SINCE": str(want_1510), "NTFY_DUP_GUARD": "1"}):
            seen_since.clear()
            # morning-sent title at 09:01 should NOT block afternoon (since=15:10)
            morning_msg = [{"event": "message", "time": int(datetime(2026, 11, 5, 9, 1, tzinfo=KST).timestamp()), "title": "[YENCARRY] USDJPY 달러 보유 줄이기."}]
            def poll_m(server, topic, since):
                seen_since.append(since)
                return [m for m in morning_msg if m["time"] >= since]
            check("dup-guard slot since skips morning title in afternoon", dup_guard_skip("cwally-port", morning_msg[0]["title"], "x", now=datetime(2026, 11, 5, 15, 25, tzinfo=KST), poll=poll_m, archive=no_arch), False)
            check("dup-guard uses DUP_GUARD_SINCE env", seen_since[-1], want_1510)
        # always-send: 설치 확인 / 테스트
        install = "[XBI] 설치 확인 | 매매 신호 아님"
        test_t = "[XBI] 테스트 | 알림 종료 2주 전 연장 점검 샘플"
        def poll_inst(server, topic, since):
            return [{"event": "message", "time": mid + 100, "title": install}, {"event": "message", "time": mid + 101, "title": test_t}]
        check("dup-guard 설치 확인 always sends", dup_guard_skip("cwally-port", install, "x", now=now, poll=poll_inst, archive=no_arch), False)
        check("dup-guard 테스트 always sends", dup_guard_skip("cwally-port", test_t, "x", now=now, poll=poll_inst, archive=no_arch), False)
        check("ALWAYS_SEND_TITLE_RE matches 설치 확인", bool(ALWAYS_SEND_TITLE_RE.search(install)), True)
        pl = json.dumps({"topic": "cwally-port", "title": t}).encode()
        with mock.patch.dict(os.environ, {"NTFY_DUP_GUARD": "1"}, clear=False):
            os.environ.pop("DUP_GUARD_SINCE", None)
            with mock.patch(f"{__name__}._dup_poll", side_effect=poll_ok), mock.patch(f"{__name__}._now_kst", return_value=now), mock.patch(f"{__name__}._dup_archive", side_effect=no_arch):
                check("dup-guard payload helper (direct posters) -> skip", dup_guard_skip_payload("x", pl), True)
                posted = []
                with mock.patch("urllib.request.urlopen", side_effect=lambda req, timeout=20: posted.append(req) or (_ for _ in ()).throw(AssertionError("network"))):
                    publish("cwally-port", t, "본문", 3, ["x"], "https://ntfy.invalid")
                check("dup-guard publish() skips POST and marks delivered", (not posted, LAST_PUBLISH.get("sent"), LAST_PUBLISH.get("dup")), (True, True, True))
        tmp = Path(os.environ.get("RUNNER_TEMP") or "/tmp") / "dupguard_archive_test.json"
        tmp.write_text(json.dumps({"items": [{"topic": "cwally-port", "time": mid + 60, "title": "A"}, {"topic": "cwally-port", "time": mid - 60, "title": "B"}]}), encoding="utf-8")
        got = [m["title"] for m in _dup_archive("cwally-port", mid, tmp)]
        tmp.unlink()
        check("dup-guard archive reader keeps only today", got, ["A"])
        # dedup_key tag helpers (used by commit 2; safe to assert now)
        tag = dedup_key_tag("20261120000001")
        check("dedup_key_tag format", bool(tag and tag.startswith("dk-") and len(tag) == 13), True)
        check("dedup_key_tag stable", dedup_key_tag("20261120000001"), tag)
        tagged = [{"event": "message", "time": mid + 50, "title": "[OTHER] x", "tags": ["chart_with_upwards_trend", tag]}]
        check("dup-guard dedup_key tag match skips", dup_guard_skip("cwally-port", "[SDI-ESS] other title", "x", now=now, poll=lambda *a: tagged, archive=no_arch, dedup_key="20261120000001"), True)
        check("dup-guard dedup_key miss falls back to title", dup_guard_skip("cwally-port", "[SDI-ESS] other title", "x", now=now, poll=lambda *a: tagged, archive=no_arch, dedup_key="other"), False)
    return before


def _self_test() -> int:
    """python scripts/ntfy_util.py self-test  (mocked HTTP; never sends)."""
    import io
    import tempfile
    from contextlib import redirect_stdout
    from unittest import mock

    global CONFIGS_DIR
    sent: list[dict] = []

    class _Resp:
        def __init__(self, req):
            sent.append(json.loads(req.data.decode()))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id":"mock"}'

    now = datetime(2026, 10, 20, 12, 0, tzinfo=KST)
    cfgs = {
        "ended": {"title_prefix": "[ENDED]", "ends_kst": "2026-10-19 15:20", "ends_note": "끝"},
        "active": {"title_prefix": "[ACTIVE]", "ends_kst": "2026-11-18 12:00", "ends_note": "x"},
        "nullend": {"title_prefix": "[NULLEND]", "ends_kst": None, "ends_note": "종료일 없음"},
        "kstsfx": {"title_prefix": "[KSTSFX]", "ends_kst": "2026-10-19 09:00 KST"},
        "dateonly": {"title_prefix": "[DATEONLY]", "ends_kst": "2026-10-20"},
        "dateold": {"title_prefix": "[DATEOLD]", "ends_kst": "2026-10-19"},
        "grace": {"title_prefix": "[GRACE]", "ends_kst": "2026-10-20 11:30"},
        "nograce": {"title_prefix": "[NOGRACE]", "ends_kst": "2026-10-20 11:30", "ends_grace_min": 0},
        "junk": {"title_prefix": "[JUNK]", "ends_kst": "11월 중순"},
        "shared1": {"title_prefix": "[SHARED]", "ends_kst": "2026-10-01 09:00"},
        "shared2": {"title_prefix": "[SHARED]", "ends_kst": "2026-12-01 09:00"},
        "multi": {"title_prefix": "[MA] [MB]", "ends_kst": "2026-10-01 09:00"},
        "noprefix": {"title_prefix": None, "ends_kst": "2026-10-01 09:00"},
    }
    old_dir, old_argv = CONFIGS_DIR, sys.argv[:]
    failures = 0

    def run(title, argv0="scripts/other.py", topic="cwally-port", env=None, **kw):
        sent.clear()
        sys.argv = [argv0]
        buf = io.StringIO()
        with mock.patch.dict(os.environ, dict(env or {}, NTFY_DUP_GUARD="0"), clear=False), mock.patch("urllib.request.urlopen", side_effect=lambda req, timeout=20: _Resp(req)), mock.patch(f"{__name__}._now_kst", return_value=now), redirect_stdout(buf):
            os.environ.pop("CWALLY_ALERT_NAME", None)
            if env and "CWALLY_ALERT_NAME" in env:
                os.environ["CWALLY_ALERT_NAME"] = env["CWALLY_ALERT_NAME"]
            if not (env and "NTFY_IGNORE_ENDS" in env):
                os.environ.pop("NTFY_IGNORE_ENDS", None)
            rv = publish(topic, title, "본문", **kw)
        return bool(sent), buf.getvalue(), rv

    def check(label, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} {label}: sent={got} want={want}")

    try:
        with tempfile.TemporaryDirectory() as td:
            CONFIGS_DIR = Path(td)
            for n, c in cfgs.items():
                (CONFIGS_DIR / f"{n}.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
            s, out, rv = run("[ENDED] 매수", "scripts/ended_monitor.py")
            check("ended alert skipped (script name)", s, False)
            check("skip line printed", "ntfy skip: ended ended 2026-10-19 15:20" in out, True)
            check("skip returns None like a send", rv is None and LAST_PUBLISH.get("sent") is False, True)
            s, out, _ = run("[ACTIVE] 매수", "scripts/active_monitor.py")
            check("active alert sent", s, True)
            check("end line still appended", "꺼지는 때: 2026-11-18 12:00 KST" in sent[0]["message"] if sent else False, True)
            check("null ends sent", run("[NULLEND] x", "scripts/nullend_monitor.py")[0], True)
            check("' KST' suffix parsed, ended", run("[KSTSFX] x", "/abs/scripts/kstsfx_monitor.py")[0], False)
            check("date-only today = end of day, still sends", run("[DATEONLY] x", "scripts/dateonly_monitor.py")[0], True)
            check("date-only yesterday skipped", run("[DATEOLD] x", "scripts/dateold_monitor.py")[0], False)
            check("within 59-min grace sends", run("[GRACE] x", "scripts/grace_monitor.py")[0], True)
            check("ends_grace_min=0 skips", run("[NOGRACE] x", "scripts/nograce_monitor.py")[0], False)
            check("unparseable fails open", run("[JUNK] x", "scripts/junk_monitor.py")[0], True)
            check("[OPS] on cwally-ops always sends", run("[OPS] 워크플로 실패 | port-alerts x", "scripts/ended_monitor.py", topic="cwally-ops")[0], True)
            check("[OPS] even from ended script on port topic", run("[OPS] x", "scripts/ended_monitor.py")[0], True)
            check("workflow failure text always sends", run("[ENDED] 워크플로 실패", "scripts/ended_monitor.py", topic="cwally-ops")[0], True)
            check("other-prefix ops notice from ended script sends", run("[ACTIVE] 조회 실패", "scripts/ended_monitor.py")[0], True)
            check("ended alert's own data-missing notice suppressed", run("[ENDED] 가격 조회 실패", "scripts/ended_monitor.py")[0], False)
            check("prefix fallback ended -> skip", run("[ENDED] x", "-c")[0], False)
            check("prefix fallback active -> send", run("[ACTIVE] x", "-c")[0], True)
            check("prefix fallback multi-prefix config", run("[MB] x", "-c")[0], False)
            check("prefix fallback derived [NAME] when title_prefix null", run("[NOPREFIX] x", "-c")[0], False)
            check("shared prefix, one still active -> send", run("[SHARED] x", "-c")[0], True)
            check("unresolvable fails open", run("[NOBODY] x", "-c")[0], True)
            check("no prefix, no script fails open", run("그냥 제목", "-c")[0], True)
            check("explicit config_name wins over script", run("[ACTIVE] x", "scripts/active_monitor.py", config_name="ended")[0], False)
            check("explicit config dict", run("[X] x", "scripts/active_monitor.py", config={"name": "d", "ends_kst": "2026-01-01 00:00"})[0], False)
            check("unknown config_name falls back to script", run("[ACTIVE] x", "scripts/active_monitor.py", config_name="zzz")[0], True)
            check("CWALLY_ALERT_NAME env resolves", run("[Q] x", "-c", env={"CWALLY_ALERT_NAME": "ended"})[0], False)
            check("_impl suffix resolves", run("[Q] x", "scripts/ended_impl.py")[0], False)
            check("NTFY_IGNORE_ENDS=1 override sends", run("[ENDED] x", "scripts/ended_monitor.py", env={"NTFY_IGNORE_ENDS": "1"})[0], True)
            s, _, _ = run("[ACTIVE] x", "scripts/active_monitor.py", append_end=False)
            check("append_end=False sends body unchanged", s and sent[0]["message"] == "본문", True)
        # record_fired: only after a real send, cap trimming
        keep_lp = dict(LAST_PUBLISH)
        st: dict = {}
        LAST_PUBLISH.clear()
        check("record_fired no publish yet", record_fired(st, "2026-11-04", "z1", 3, "[X] t") or "fired" in st, False)
        LAST_PUBLISH.update({"sent": False, "reason": "ended", "title": "[X] t"})
        check("record_fired skipped send", record_fired(st, "2026-11-04", "z1", 3, "[X] t") or "fired" in st, False)
        LAST_PUBLISH.update({"sent": True, "reason": "", "title": "[X] t"})
        ok = record_fired(st, "2026-11-04", "hi52+tnx_hi", 3, "[X] t", datetime(2026, 11, 5, 9, tzinfo=timezone(timedelta(hours=9))))
        check("record_fired real send", ok and st["fired"] == [{"date": "2026-11-04", "key": "hi52+tnx_hi", "priority": 3, "title": "[X] t", "sent_at": "2026-11-05T09:00:00+09:00"}], True)
        for k in range(70):
            record_fired(st, f"d{k}", "z2", 3, "[X] x", cap=60)
        check("record_fired cap 60", len(st["fired"]) == 60 and st["fired"][-1]["date"] == "d69" and st["fired"][0]["date"] == "d10", True)
        LAST_PUBLISH.clear()
        LAST_PUBLISH.update(keep_lp)
        failures += _dup_guard_selftest(check)
        # selftest guard: script without its own selftest stops at import (exit 2), one with it runs
        import subprocess
        tmpd = Path(tempfile.mkdtemp())
        here = str(Path(__file__).resolve().parent)
        (tmpd / "plain_monitor.py").write_text(f"import sys; sys.path.insert(0, {here!r}); import ntfy_util; print('RAN')\n", encoding="utf-8")
        (tmpd / "own_monitor.py").write_text(f"import sys; sys.path.insert(0, {here!r}); import ntfy_util  # selftest mode below\nprint('RAN')\n", encoding="utf-8")
        env0 = {k: v for k, v in os.environ.items() if k != "CWALLY_SELFTEST_OK"}
        r1 = subprocess.run([sys.executable, str(tmpd / "plain_monitor.py"), "selftest"], capture_output=True, text=True, env=env0)
        check("guard stops script without selftest", r1.returncode == 2 and "RAN" not in r1.stdout and SELFTEST_STOP_MSG in r1.stderr, True)
        r2 = subprocess.run([sys.executable, str(tmpd / "own_monitor.py"), "selftest"], capture_output=True, text=True, env=env0)
        check("guard lets own-selftest script run", r2.returncode == 0 and "RAN" in r2.stdout, True)
        r3 = subprocess.run([sys.executable, str(tmpd / "plain_monitor.py"), "auto"], capture_output=True, text=True, env=env0)
        check("guard ignores auto", r3.returncode == 0 and "RAN" in r3.stdout, True)
    finally:
        CONFIGS_DIR, sys.argv = old_dir, old_argv
    print("SELF-TEST " + ("PASS" if not failures else f"FAIL ({failures})"))
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["self-test"]:
        raise SystemExit(_self_test())
    print("usage: python scripts/ntfy_util.py self-test", file=sys.stderr)
    raise SystemExit(2)
