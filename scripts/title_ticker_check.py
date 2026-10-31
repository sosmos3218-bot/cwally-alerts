#!/usr/bin/env python3
"""Flag ntfy titles that do not name a ticker.

A title passes when it contains a ticker-like token: a KRX code, a known
symbol (including one that is the whole bracket prefix, or that the prefix
starts with, such as [XBI] or [FIG-PX]), or a {ticker} / code placeholder.
The token sits after the bracket when the prefix itself is not the symbol.

Skipped on purpose (allowlist): poly, polyinv, ghtrend, sgyn, gval, brief,
burry, ops notices, and any alert whose ends_kst is already past.

  python scripts/title_ticker_check.py
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
PLACE_RE = re.compile(r"\{ticker\}|\{co\[['\"]code['\"]\]\}|\[['\"]code['\"]\]")
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


def main() -> int:
    self_check()
    now = datetime.now(KST)
    problems = scan(now)
    if problems:
        print(f"title ticker check FAIL ({len(problems)})")
        for row in problems:
            print(row)
        return 1
    print(f"title ticker check PASS ({now:%Y-%m-%d %H:%M KST})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
