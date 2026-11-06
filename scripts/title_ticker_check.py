#!/usr/bin/env python3
"""Flag ntfy titles that do not name a ticker.

A title passes when it contains a ticker-like token: a KRX code, a known
symbol (including one that is the whole bracket prefix, or that the prefix
starts with, such as [XBI] or [FIG-PX]), or a {ticker} / code placeholder.
The token sits after the bracket when the prefix itself is not the symbol.

Skipped on purpose (allowlist): poly, polyinv, ghtrend, sgyn, gval, brief,
burry, ops notices, and any alert whose ends_kst is already past.

  python scripts/title_ticker_check.py            # blocking check, then report-only audit
  (default: static audit violations, code + unsent outbox, also exit 1)
  python scripts/title_ticker_check.py --report-only  # audit never blocks (or TITLE_TICKER_REPORT_ONLY=1)
  python scripts/title_ticker_check.py --strict   # sent-archive violations also exit 1 (or TITLE_TICKER_STRICT=1)
  python scripts/title_ticker_check.py --no-audit # blocking check only
Legacy archive ids (pt80u): ARCHIVE_LEGACY_IDS skips three 2026-10-27~29 sends that predate the ticker-in-title rule.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / "configs"
SCRIPTS = ROOT / "scripts"
KST = timezone(timedelta(hours=9))

# No single instrument, or the owner asked to leave the alert alone.
ALLOW_STEMS = {"poly", "polyinv", "ghtrend", "sgyn", "gval", "brief", "burry", "ops_notify"}

# Index and macro symbols, plus tickers that are not a config "ticker" field.
EXTRA_SYMBOLS = {
    "KOSPI", "KOSDAQ", "KOSPI200", "SPY", "USDKRW", "USDJPY", "VGK",
    "IEF", "NEE", "SO", "DUK", "VTR", "SPG", "BTC", "ETH", "XRP", "G",
    "NVDA", "SNDK", "CRWV", "FIG", "NFLX", "IREN", "PLTR", "PCOR", "AMTM",
    "TSLA", "SPCX", "WBTN", "QQQ",
}
SPECIALS = ("BZ=F", "^TNX", "^N225", "^KS200", "^GSPC", "^SOX")

# Bracket text that is a nickname, not the tradable symbol. A 1-5 letter prefix
# outside this set is the symbol itself ([VYM], [ASML], [XBI]).
NON_SYMBOL = {
    "ALUX", "BRIEF", "BURRY", "CNSDS", "DEAR", "EARN", "FLOW", "GSAI", "GVAL",
    "HCE", "JYP", "KICA", "KOSUP", "KRWFX", "KSHIP", "KZ", "LG", "LGES", "LGESF",
    "LGIT", "MANH", "MERAS", "MKIF", "OIL", "PEPT", "POLY", "PORT", "RATES",
    "BOK", "REMED", "SAMG", "SDI", "SEC", "SECQ3", "SGYN", "SHI", "SKC",
    "SKINC", "STEEL", "TRUEN", "USTSY",
}

KR_RE = re.compile(r"(?<![A-Z0-9])(\d{6}|\d{4}[A-Z][A-Z0-9])(?![A-Z0-9])")
PLACE_RE = re.compile(r"\{ticker\}|\{tk\}|join\(tks\)|\{co\[['\"]code['\"]\]\}|\[['\"]code['\"]\]")
OPEN_RE = re.compile(r"""(?<![\'\"])(?P<pre>f?['\"])\[(?P<inner>[^\]]+)\]""")
VAR_RE = re.compile(r"""f(?P<q>['\"])\{(?P<var>TAG|NAME|PREFIX|P|prefix)\}""")
CFG_RE = re.compile(r"""f(?P<q>['\"])\{cfg\[['\"]title_prefix['\"]\]\}""")
CAT_RE = re.compile(r"""cfg\[['\"]title_prefix['\"]\]\s*\+\s*(?P<q>['\"])""")


def known_symbols() -> set[str]:
    found = set(EXTRA_SYMBOLS)
    for path in CONFIGS.glob("*.json"):
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(cfg, dict):
            _collect(cfg, found)
    return found


def _collect(node, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, val in node.items():
            if key in {"ticker", "symbol", "code", "pref_code", "common_code", "etf_code"} and isinstance(val, str):
                sym = val.split(".")[0].strip()
                if re.fullmatch(r"[A-Z][A-Z0-9]{0,5}", sym):
                    found.add(sym)
            else:
                _collect(val, found)
    elif isinstance(node, list):
        for item in node:
            _collect(item, found)


KNOWN = known_symbols()


def prefix_is_ticker(inner: str) -> bool:
    if KR_RE.fullmatch(inner):
        return True
    if inner in KNOWN:
        return True
    if re.fullmatch(r"[A-Z]{1,5}", inner) and inner not in NON_SYMBOL:
        return True
    for sym in KNOWN:
        if len(sym) >= 3 and inner.startswith(sym):
            return True
    return False


def has_symbol(text: str) -> bool:
    for sym in KNOWN:
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(sym) + r"(?![A-Za-z0-9])", text):
            return True
    return False


def has_ticker(text: str) -> bool:
    if KR_RE.search(text) or PLACE_RE.search(text) or has_symbol(text):
        return True
    if any(sym in text for sym in SPECIALS):
        return True
    for m in re.finditer(r"\[([^\]]+)\]", text):
        if prefix_is_ticker(m.group(1)):
            return True
    return False


def read_rest(text: str, pos: int, quote: str) -> str:
    buf = []
    depth = 0
    for ch in text[pos:]:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == quote and depth == 0:
            break
        elif ch == "\n" and depth == 0:
            break
        buf.append(ch)
    return "".join(buf)


def parse_ends(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = re.sub(r"\s*\(?KST\)?$", "", raw.strip(), flags=re.I).replace("T", " ").replace("/", "-")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=KST) + timedelta(days=1)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def ended(cfg: dict, now: datetime) -> bool:
    end = parse_ends(cfg.get("ends_kst"))
    if end is None:
        return False
    try:
        grace = int(cfg.get("ends_grace_min", 59))
    except (TypeError, ValueError):
        grace = 59
    return now > end + timedelta(minutes=max(grace, 0))


def load_cfg(stem: str) -> dict | None:
    path = CONFIGS / f"{stem}.json"
    if not path.is_file():
        return None
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cfg if isinstance(cfg, dict) else None


def brackets(title_prefix) -> list[str]:
    if not isinstance(title_prefix, str):
        return []
    return re.findall(r"\[[^\]]+\]", title_prefix)


def assigned(text: str, var: str, prefix: str) -> bool:
    return re.search(rf"^{re.escape(var)}\s*=\s*['\"]{re.escape(prefix)}['\"]\s*$", text, re.M) is not None


def templates(text: str, prefix: str) -> list[tuple[int, str]]:
    inner = prefix[1:-1]
    out = []
    for m in OPEN_RE.finditer(text):
        if m.group("inner") != inner:
            continue
        quote = m.group("pre")[-1]
        rest = read_rest(text, m.end(), quote)
        if not rest.strip():
            continue  # prefix constant, e.g. TAG = "[SQUARE]"
        line = text[: m.start()].count("\n") + 1
        out.append((line, prefix + rest))
    for m in VAR_RE.finditer(text):
        var = m.group("var")
        if var == "prefix":
            if not re.search(r"^prefix\s*=\s*cfg\[.title_prefix.\]", text, re.M):
                continue
        elif not assigned(text, var, prefix):
            continue
        rest = read_rest(text, m.end(), m.group("q"))
        line = text[: m.start()].count("\n") + 1
        out.append((line, prefix + rest))
    for cre in (CFG_RE, CAT_RE):
        for m in cre.finditer(text):
            rest = read_rest(text, m.end(), m.group("q"))
            line = text[: m.start()].count("\n") + 1
            out.append((line, prefix + rest))
    return out


def scan(now: datetime) -> list[str]:
    problems = []
    files = list(SCRIPTS.glob("*_monitor.py"))
    files.append(SCRIPTS / "square_sell_alert.mjs")
    for path in files:
        stem = "square" if path.name == "square_sell_alert.mjs" else path.name.split("_monitor.py")[0]
        if stem in ALLOW_STEMS or path.stem in ALLOW_STEMS:
            continue
        cfg = load_cfg(stem)
        if cfg is None or "title_prefix" not in cfg:
            continue
        if ended(cfg, now):
            continue
        prefs = brackets(cfg.get("title_prefix"))
        if not prefs:
            continue
        text = path.read_text(encoding="utf-8")
        seen = 0
        for prefix in prefs:
            found = templates(text, prefix)
            seen += len(found)
            for line, title in found:
                if not has_ticker(title):
                    problems.append(f"{path.name}:{line}: {title[:140]}")
        if seen == 0 and not all(prefix_is_ticker(p[1:-1]) for p in prefs):
            problems.append(f"{path.name}: no title template found for {prefs[0]}")
    for path in sorted(CONFIGS.glob("*.json")):
        cfg = load_cfg(path.stem)
        if cfg is None or path.stem in ALLOW_STEMS or ended(cfg, now):
            continue
        prefs = set(brackets(cfg.get("title_prefix")))
        if not prefs:
            continue
        for key_title in _title_strings(cfg):
            for prefix in prefs:
                if key_title.startswith(prefix) and not has_ticker(key_title):
                    problems.append(f"configs/{path.name}: {key_title[:140]}")
    return problems


def _title_strings(node) -> list[str]:
    out = []
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "title" and isinstance(val, str):
                out.append(val)
            else:
                out.extend(_title_strings(val))
    elif isinstance(node, list):
        for item in node:
            out.extend(_title_strings(item))
    return out


def self_check() -> None:
    assert has_ticker("[GOOGL] 유지")
    assert has_ticker("[005930] 종가 220,000")
    assert has_ticker("[0220WL] 보유")
    assert has_ticker("[FIG-PX] 설치 확인 | 매매 신호 아님")
    assert has_ticker("[XRP-ETF] 주간")
    assert has_ticker("[SQUARE] 402340 보유 유지")
    assert has_ticker("[RATES] ^TNX 10년물")
    assert has_ticker("[OIL] BZ=F 브렌트")
    assert has_ticker("[ADDSTOP] {ticker} 점검 대기")
    assert has_ticker("[KR-HC-RPT] {co['code']} 셀트리온")
    assert not has_ticker("[SQUARE] 보유 유지")
    assert not has_ticker("[FLOW] 과열 조짐 | 추격 금지")
    assert not has_ticker("[SEC-DIV] 배당 공시")


# ---------------------------------------------------------------------------
# Report-only audit (2026-11-05, t108u). Wider scope + strict ticker judgement.
# Does not change the exit code unless --strict or TITLE_TICKER_STRICT=1.
# ---------------------------------------------------------------------------
import ast  # noqa: E402
import os  # noqa: E402

# Real tickers / index symbols that are not in a config ticker/code field.
AUDIT_EXTRA = {
    "ASML", "ISRG", "FTNT", "VYM", "SOXX", "SMH", "SOX", "K200", "KOSPI200", "DIA", "IWM",
    "TNX", "DXY", "VIX", "WTI", "BRENT", "GOLD", "USDKRW", "JPYKRW",
}
# Nickname prefix -> real ticker. An alias never satisfies the rule by itself:
# the real ticker must still appear in the title. Used only to say what is missing.
TICKER_ALIASES = {"CRWVBOND": "CRWV", "PLTR25": "PLTR"}
# Prefixes whose alert is market-wide (needs an index ticker such as KOSPI, SPY, ^TNX).
MARKET_WIDE = {
    "FLOW", "MARKET", "RATES", "TREND", "PORT", "PORTLIGHT", "POLY", "POLYINV", "GHTREND", "K200",
    "MACRO-IR", "KR-RATE", "US-DIV", "US-BOND", "YENCARRY", "KRWFX", "BOK", "FR-DE", "KOSPI",
    "KOS5500", "KOSUP", "KOSDOWN", "KOSBRK", "Q3FLOW", "MEMSWING", "MEMWATCH", "GVAL", "COUNCIL",
}
FREE_TEXT = {"BRIEF", "EARN"}
TITLE_NAME_RE = re.compile(r"title|head|prefix|tag", re.I)
SEND_FUNC_RE = re.compile(r"publish|send|post|notify|ntfy", re.I)
PREFIX_RE = re.compile(r"^\[([^\]\n]{1,40})\]")
PREFIX_INNER_OK = re.compile(r"^[A-Z0-9가-힣{][A-Za-z0-9가-힣\-_ {}.\[\]'\"()]*$")
TEST_FUNC_RE = re.compile(r"^(selftest|self_test|_self_test|_selftest|self_check|audit_self_check|test_.*|_test.*|run_selftest.*)$")
# DART filing title markers such as [정정] / [기재정정]: disclosure names, not ntfy titles.
FILING_MARKERS = {"정정", "기재정정", "첨부정정", "발행조건확정", "첨부추가"}
TICKER_PLACE_RE = re.compile(r"\{[^}]*(ticker|tk|tks|code|sym|symbol|stock_code)[^}]*\}", re.I)
_SYMS: set | None = None


def _audit_syms() -> set[str]:
    global _SYMS
    if _SYMS is None:
        _SYMS = {s for s in (set(KNOWN) | AUDIT_EXTRA) if len(s) >= 2} | {"G"}  # Genpact (NYSE: G); other 1-letter tokens are too noisy
    return _SYMS


def strict_has_ticker(text: str) -> bool:
    """Real ticker only: KRX code, config/extra symbol as a whole token, special symbol, or ticker placeholder."""
    if KR_RE.search(text) or PLACE_RE.search(text) or TICKER_PLACE_RE.search(text):
        return True
    if any(s in text for s in SPECIALS):
        return True
    for sym in _audit_syms():
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(sym) + r"(?![A-Za-z0-9])", text):
            return True
    return False


def _is_ops_title(t: str) -> bool:
    try:
        sys.path.insert(0, str(SCRIPTS))
        from ntfy_util import OPS_TITLE_RE  # type: ignore
    except Exception:  # noqa: BLE001
        OPS_TITLE_RE = re.compile(r"조회\s?실패|수집\s?실패|파싱\s?실패|해석\s?불가|확인 불가|미게시")
    return t.startswith("[OPS]") or bool(OPS_TITLE_RE.search(t))


def _render(node, consts: dict) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                inner = v.value
                if isinstance(inner, ast.Name) and inner.id in consts:
                    out.append(consts[inner.id])
                else:
                    out.append("{" + ast.unparse(inner) + "}")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _render(node.left, consts), _render(node.right, consts)
        if a is not None and b is not None:
            return a + b
        if a is not None:
            return a + "{...}"
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


def _module_consts(tree) -> dict:
    consts: dict = {}
    for _ in range(2):
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                r = _render(n.value, consts)
                if r is not None and "{" not in r:
                    consts[n.targets[0].id] = r
    return consts


def _title_like(t: str) -> bool:
    m = PREFIX_RE.match(t)
    if not m or not PREFIX_INNER_OK.match(m.group(1)) or m.group(1).strip() in FILING_MARKERS:
        return False
    rest = t[m.end():].strip()
    return bool(rest)  # bare prefix constants (TAG = "[X]") are not titles


def py_title_candidates(path: Path) -> list[tuple[int, str]]:
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return []
    consts = _module_consts(tree)
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    out: list[tuple[int, str]] = []
    seen_nodes: set = set()
    for n in ast.walk(tree):
        if not isinstance(n, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            continue
        if n in seen_nodes or isinstance(parents.get(n), (ast.JoinedStr, ast.BinOp)):
            continue
        r = _render(n, consts)
        if r is None or not _title_like(r):
            continue
        anc, in_test = parents.get(n), False
        while anc is not None:
            if isinstance(anc, (ast.FunctionDef, ast.AsyncFunctionDef)) and TEST_FUNC_RE.match(anc.name):
                in_test = True
                break
            anc = parents.get(anc)
        if in_test:
            continue  # selftest fixtures are not sent titles
        p = parents.get(n)
        flows = False
        if isinstance(p, ast.Assign) and any(isinstance(t, ast.Name) and TITLE_NAME_RE.search(t.id) for t in p.targets):
            flows = True
        elif isinstance(p, ast.keyword) and p.arg and TITLE_NAME_RE.search(p.arg):
            flows = True
        elif isinstance(p, ast.Call):
            fn = ast.unparse(p.func)
            if SEND_FUNC_RE.search(fn) or fn.endswith("append"):
                flows = True
        elif isinstance(p, ast.Tuple) and p.elts and p.elts[0] is n:
            flows = True  # (title, body, priority) alert tuples
        elif isinstance(p, ast.Dict):
            flows = True  # dict of titles
        elif isinstance(p, (ast.Return, ast.IfExp)):
            flows = True
        if flows:
            out.append((n.lineno, r))
    return out


MJS_RE = re.compile(r"""(?:`|'|")(\[[^\]\n]{1,40}\][^`'"\n]*)""")


def mjs_title_candidates(path: Path) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for m in MJS_RE.finditer(line):
            t = re.sub(r"\$\{([^}]*)\}", r"{\1}", m.group(1))
            if _title_like(t):
                out.append((i, t))
    return out


def _stem_of(path: Path) -> str:
    n = path.name
    for suf in ("_monitor.py", "_impl.py", ".py", ".mjs"):
        if n.endswith(suf):
            return n[: -len(suf)]
    return path.stem


def classify(title: str) -> str | None:
    """None = passes. Otherwise a reason/category."""
    if _is_ops_title(title):
        return "excluded ops"
    if strict_has_ticker(title):
        return None
    pre = PREFIX_RE.match(title).group(1) if PREFIX_RE.match(title) else ""
    if pre.startswith("{"):
        return "dynamic, not checkable"
    rest = title[len(pre) + 2:].strip() if pre else title
    if pre in FREE_TEXT and re.fullmatch(r"(\{[^}]*\}[\s|:()/0-9-]*)+", rest or "{x}"):
        return "dynamic, not checkable"
    if pre in MARKET_WIDE:
        return "market-wide: no index ticker"
    if pre in FREE_TEXT:
        return "free-text: no ticker"
    alias = TICKER_ALIASES.get(pre)
    return f"stock nickname: no ticker{f' (alias of {alias}, real ticker must appear)' if alias else ''}"


# Non-investment alerts, excluded from the audit by exact script stem (pt110u, 2026-11-05).
# gval: AI API price comparison. ghtrend: GitHub trending repos. Neither measures a market.
# Sent archive titles are matched only by the exact title_prefix in these stems' configs.
AUDIT_NON_INVEST = {"gval", "ghtrend"}


def _non_invest_prefixes() -> set[str]:
    out = set()
    for stem in AUDIT_NON_INVEST:
        cfg = load_cfg(stem) or {}
        if cfg.get("title_prefix"):
            out.add(cfg["title_prefix"])
    return out


# Legacy sent-archive exceptions (pt80u, 2026-11-06). Exact ntfy message ids only.
# These three went out 2026-10-27~29 before the title-must-include-ticker rule took effect
# (scripts/title_ticker_check.py via #32 / 68c9e72 around 2026-10-31). Tickers were already
# in the body first line (005930 / 006400 / 053300). Do not extend without owner approval;
# new sends must put the ticker in the title. Archived messages themselves are not edited.
ARCHIVE_LEGACY_IDS = {
    "PvIt7pE2wfAw",  # 2026-10-28 09:01 KST [SEC] 연간치 하향 | 3Q 83.0조
    "sB9JBv1mcchR",  # 2026-10-27 16:20 KST [SDI] 보유 | 변경 조건 미충족
    "nzFLIyCwGJKt",  # 2026-10-29 15:41 KST [KICA] 5,090원 -5.9% | 하루 -5.9% 하락
}

# Static audit exceptions (pt111u, 2026-11-05). Each entry is (file, title regex, reason).
# Matched by file path relative to the repo plus a stable title pattern, never by line number.
# New entries need owner approval. New outbox briefs must carry a ticker instead.
AUDIT_EXCEPTIONS = [
    ("outbox/20261028-lgesfor-timing.json", r"^\[BRIEF\] lgesfor 19시 데이터 시차 판정", "sent brief"),
    ("outbox/20261029-earn-skh-msft-googl-1.json", r"^\[EARN\] 10/28 SK하이닉스", "sent brief"),
    ("outbox/20261029-earn-skh-msft-googl-2.json", r"^\[EARN\] 10/28 SK하이닉스", "sent brief"),
    ("outbox/20261030-kospi-rebound-1.json", r"^\[BRIEF\] 실적 후 반등 성격 판정", "sent brief"),
    ("outbox/20261030-kospi-rebound-2.json", r"^\[BRIEF\] 실적 후 반등 성격 판정", "sent brief"),
    ("outbox/20261030-kospi-rebound-3.json", r"^\[BRIEF\] 실적 후 반등 성격 판정", "sent brief"),
    ("outbox/20261030-kospi-rebound-4.json", r"^\[BRIEF\] 실적 후 반등 성격 판정", "sent brief"),
    ("outbox/20261104-frde-event.json", r"^\[FR-DE\] 하원 PLF 1부 표결 연기", "sent brief"),
    # prefix comes from label[c['name']], title body from strip_prefix(c['title'])
    ("scripts/kospi_monitor.py", r"^\[\{label\[c\['name'\]\]\}\] \{strip_prefix\(c\['title'\]\)\}$", "dynamic title"),
    # year/span bracket built per run
    ("scripts/usdiv_monitor.py", r"^\[\{y\}년 \{span\}\] ", "dynamic title"),
    # "[TREND] " + ", ".join(named): named rows are "{ticker} {name}"
    ("trend_monitor/ntfy_summary.py", r"^\[TREND\] \{\.\.\.\}$", "false positive, tickers at runtime"),
]


def _exception_reason(rel: str, title: str) -> str | None:
    for f, pat, reason in AUDIT_EXCEPTIONS:
        if rel == f and re.search(pat, title):
            return reason
    return None


def audit_files() -> list[Path]:
    files = sorted(set(SCRIPTS.rglob("*.py")) | set(SCRIPTS.rglob("*.mjs")))
    tm = ROOT / "trend_monitor"
    if tm.is_dir():
        files += sorted(tm.rglob("*.py"))
    skip = {"title_ticker_check.py", "ntfy_util.py"}
    return [f for f in files if f.name not in skip and "fixtures" not in f.parts and not f.name.startswith("test_")]


def audit(now: datetime) -> dict:
    res: dict = {"static": [], "excluded": [], "archive": [], "archive_body_only": [], "archive_excluded": 0,
           "archive_legacy": 0, "non_invest": 0, "exceptions": []}
    ni_prefixes = _non_invest_prefixes()
    for path in audit_files():
        stem = _stem_of(path)
        cfg = load_cfg(stem)
        if cfg is not None and ended(cfg, now):
            continue
        if stem in AUDIT_NON_INVEST:
            res["non_invest"] += len(py_title_candidates(path)) if path.suffix == ".py" else 0
            continue
        cands = mjs_title_candidates(path) if path.suffix == ".mjs" else py_title_candidates(path)
        for line, t in cands:
            why = classify(t)
            rel = path.relative_to(ROOT)
            if why == "excluded ops":
                res["excluded"].append(f"{rel}:{line}: {t[:120]}")
            elif why and _exception_reason(rel.as_posix(), t):
                res["exceptions"].append((f"{rel}:{line}", _exception_reason(rel.as_posix(), t)))
            elif why:
                res["static"].append((f"{rel}:{line}", t[:140], why))
    ob = ROOT / "outbox"
    for path in sorted(ob.glob("*.json")) if ob.is_dir() else []:
        try:
            t = json.loads(path.read_text(encoding="utf-8")).get("title") or ""
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        why = classify(t) if t else None
        if why == "excluded ops":
            res["excluded"].append(f"{path.relative_to(ROOT)}: {t[:120]}")
        elif why and _exception_reason(path.relative_to(ROOT).as_posix(), t):
            res["exceptions"].append((str(path.relative_to(ROOT)), _exception_reason(path.relative_to(ROOT).as_posix(), t)))
        elif why:
            res["static"].append((str(path.relative_to(ROOT)), t[:140], why))
    arch = ROOT / "state" / "archive.json"
    try:
        items = json.loads(arch.read_text(encoding="utf-8")).get("items") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        items = []
    for it in items:
        t = it.get("title") or ""
        if it.get("id") in ARCHIVE_LEGACY_IDS:
            res["archive_legacy"] += 1
            continue
        if any(t.startswith(p + " ") or t == p for p in ni_prefixes):
            res["non_invest"] += 1
            continue
        why = classify(t)
        if why is None:
            continue
        if why == "excluded ops" or it.get("topic") == "cwally-ops":
            res["archive_excluded"] += 1
            continue
        first = (it.get("message") or "").split("\n")[0]
        when = datetime.fromtimestamp(int(it.get("time") or 0), KST).strftime("%m-%d %H:%M")
        row = (f"archive {when} {it.get('topic')}", t[:140], why)
        if strict_has_ticker(first):
            res["archive_body_only"].append((row[0], row[1], "ticker only in body first line"))
        else:
            res["archive"].append(row)
    return res


def print_audit(res: dict, blocking: bool = True) -> int:
    """Prints the audit. Returns the STATIC violation count (the blocking part)."""
    from collections import Counter
    st = res["static"]
    cats = Counter(r[2].split(" (")[0] for r in st)
    n = len(st) + len(res["archive"])
    mode = "static blocks" if blocking else "report-only"
    print(f"title ticker audit ({mode}; sent and body-first-line are report-only): {n} violations "
          f"(static {len(st)}, sent {len(res['archive'])}; body-first-line only {len(res['archive_body_only'])}; "
          f"excluded ops {len(res['excluded']) + res['archive_excluded']}; "
          f"excluded non-invest {res.get('non_invest', 0)}; "
          f"excluded exception {len(res.get('exceptions', []))}; "
          f"excluded legacy-archive {res.get('archive_legacy', 0)})")
    for k, v in sorted(cats.items()):
        print(f"  category {k}: {v}")
    for loc, t, why in st:
        print(f"STATIC {loc}: {t} | {why}")
    for loc, t, why in res["archive"] + res["archive_body_only"]:
        print(f"report-only {loc}: {t} | {why}")
    return len(st), len(res["archive"])


def audit_self_check() -> None:
    assert classify("[ZZTEST] 아무 종목 없음 | 확인").startswith("stock nickname")
    assert classify("[GVAL] 일상 | 유지").startswith("market-wide")
    assert classify("[HCE] 주간 체크 | 이탈") .startswith("stock nickname")
    assert classify("[SEMIUS] 005930 000660 META 하향") is None
    assert classify("[ASML] $1,563 이하") is None
    assert classify("[FLOW] KOSPI 수급 평온") is None
    assert classify("[OPS] 워크플로 실패") == "excluded ops"
    assert classify("[XBI] 종가 조회 실패 | 판정 못 함") == "excluded ops"
    assert classify("[BRIEF] {title}") == "dynamic, not checkable"
    assert classify("[CRWVBOND] 회사채 78.8").endswith("real ticker must appear)")
    assert classify("[ADDSTOP] {ticker} 점검") is None
    assert classify("[GENPACT] G 대기") is None
    assert _exception_reason("outbox/20261104-frde-event.json", "[FR-DE] 하원 PLF 1부 표결 연기 | 영향 제한") == "sent brief"
    assert _exception_reason("outbox/20261199-new.json", "[FR-DE] 하원 PLF 1부 표결 연기") is None
    assert _exception_reason("trend_monitor/ntfy_summary.py", "[TREND] {...}") is not None
    assert _exception_reason("trend_monitor/ntfy_summary.py", "[TREND] 시장폭 회복") is None
    assert "nzFLIyCwGJKt" in ARCHIVE_LEGACY_IDS and "PvIt7pE2wfAw" in ARCHIVE_LEGACY_IDS and "sB9JBv1mcchR" in ARCHIVE_LEGACY_IDS
    assert len(ARCHIVE_LEGACY_IDS) == 3  # pt80u: do not grow without owner approval


def main() -> int:
    self_check()
    now = datetime.now(KST)
    problems = scan(now)
    strict = "--strict" in sys.argv[1:] or os.environ.get("TITLE_TICKER_STRICT") == "1"
    report_only = "--report-only" in sys.argv[1:] or os.environ.get("TITLE_TICKER_REPORT_ONLY") == "1"
    rc = 0
    if problems:
        print(f"title ticker check FAIL ({len(problems)})")
        for row in problems:
            print(row)
        rc = 1
    else:
        print(f"title ticker check PASS ({now:%Y-%m-%d %H:%M KST})")
    if "--no-audit" not in sys.argv[1:]:
        audit_self_check()
        n_static, n_sent = print_audit(audit(now), blocking=not report_only)
        if n_static and not report_only:
            print(f"title ticker audit BLOCK: static {n_static} -> exit 1")
            rc = 1
        if strict and n_sent:
            print(f"title ticker audit STRICT: sent {n_sent} -> exit 1")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
