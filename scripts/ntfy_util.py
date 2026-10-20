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
ALWAYS_SEND_TITLE_RE = re.compile(r"^\s*\[OPS\]|워크플로\s?(실패|검사 실패)")
_PREFIX_RE = re.compile(r"^\s*(\[[^\]]+\])")
LAST_PUBLISH: dict = {}


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
    topic = route_topic(topic, title)
    skip = ends_skip_reason(topic, title, config_name=config_name, config=config)
    if skip:
        print(skip)
        LAST_PUBLISH.clear()
        LAST_PUBLISH.update({"sent": False, "reason": skip, "title": title})
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
        with mock.patch.dict(os.environ, env or {}, clear=False), mock.patch("urllib.request.urlopen", side_effect=lambda req, timeout=20: _Resp(req)), mock.patch(f"{__name__}._now_kst", return_value=now), redirect_stdout(buf):
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
    finally:
        CONFIGS_DIR, sys.argv = old_dir, old_argv
    print("SELF-TEST " + ("PASS" if not failures else f"FAIL ({failures})"))
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["self-test"]:
        raise SystemExit(_self_test())
    print("usage: python scripts/ntfy_util.py self-test", file=sys.stderr)
    raise SystemExit(2)
