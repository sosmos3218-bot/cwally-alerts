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
CFG_PATH = ROOT / "configs" / "muq4.json"
STATE_PATH = ROOT / "state" / "muq4.json"
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


def emit(cfg: dict, title: str, lines: list[str], priority: int = 4) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["chart_with_upwards_trend"], server)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text))


def parse_money(text: str, patterns: list[str]):
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        raw = m.group(1).replace(",", "")
        try:
            return float(raw)
        except ValueError:
            continue
    return None


def parse_pr(text: str) -> dict:
    published = bool(
        re.search(r"fiscal fourth quarter|fourth quarter of fiscal 2026|Q4 FY ?2026", text, re.I)
        and re.search(r"(revenue|net sales|diluted EPS|earnings per share)", text, re.I)
        and re.search(r"September 30, 2026|Sep(?:tember)? 30, 2026|ended August", text, re.I)
        and not re.search(r"will hold its fiscal fourth quarter earnings", text, re.I)
    )
    rev = parse_money(
        text,
        [
            r"revenue (?:was|of|reached) \$?([0-9]+(?:\.[0-9]+)?)\s*billion",
            r"net sales (?:were|of) \$?([0-9]+(?:\.[0-9]+)?)\s*billion",
            r"total revenue of \$?([0-9]+(?:\.[0-9]+)?)\s*billion",
        ],
    )
    eps = parse_money(
        text,
        [
            r"non-?GAAP diluted EPS (?:was|of) \$?([0-9]+(?:\.[0-9]+)?)",
            r"adjusted diluted EPS (?:was|of) \$?([0-9]+(?:\.[0-9]+)?)",
            r"diluted EPS (?:was|of) \$?([0-9]+(?:\.[0-9]+)?)",
            r"earnings per share of \$?([0-9]+(?:\.[0-9]+)?)",
        ],
    )
    guide_rev = parse_money(
        text,
        [
            r"first quarter.{0,80}revenue.{0,40}\$?([0-9]+(?:\.[0-9]+)?)\s*billion",
            r"Q1 FY ?2027.{0,80}revenue.{0,40}\$?([0-9]+(?:\.[0-9]+)?)\s*billion",
        ],
    )
    sentences = re.split(r"(?<=[\.!?])\s+", text)
    hbm_hits = []
    weak = False
    for s in sentences:
        if not re.search(r"\bHBM|high[- ]bandwidth", s, re.I):
            continue
        clip = s.strip()[:180]
        if clip and clip not in hbm_hits:
            hbm_hits.append(clip)
        if re.search(r"slow|weak|delay|push.?out|inventory|cut|below|shortfall|soften", s, re.I):
            weak = True
    return {
        "published": published,
        "rev_bn": rev,
        "eps": eps,
        "guide_rev_bn": guide_rev,
        "hbm": " / ".join(hbm_hits[:3]) if hbm_hits else "발표문에서 HBM 문장 확인 불가",
        "hbm_weak": weak,
        "hbm_found": bool(hbm_hits),
    }


def yahoo_quote(symbol: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    raw = json.loads(fetch(url))
    result = ((raw.get("chart") or {}).get("result") or [None])[0] or {}
    meta = result.get("meta") or {}
    px = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    post = meta.get("postMarketPrice")
    chg = None
    if isinstance(px, (int, float)) and isinstance(prev, (int, float)) and prev:
        chg = (px / prev - 1.0) * 100.0
    ah = None
    if isinstance(post, (int, float)) and isinstance(px, (int, float)) and px:
        ah = (post / px - 1.0) * 100.0
    return {"px": px, "prev": prev, "chg": chg, "post": post, "ah": ah}


def fmt_n(v, suffix=""):
    return f"{v:,.2f}{suffix}" if isinstance(v, (int, float)) else "확인 불가"


def fmt_pct(v):
    if not isinstance(v, (int, float)):
        return "확인 불가"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"


def vs_cons(actual, cons):
    if not isinstance(actual, (int, float)) or not isinstance(cons, (int, float)):
        return "확인 불가"
    if actual >= cons:
        return f"상회 +{actual - cons:.2f}"
    return f"하회 {actual - cons:.2f}"


def decide(pr: dict, kr: dict) -> str:
    if not pr.get("published"):
        return "WATCH"
    miss = False
    if isinstance(pr.get("rev_bn"), float) and pr["rev_bn"] < 50.45:
        miss = True
    if isinstance(pr.get("eps"), float) and pr["eps"] < 31.43:
        miss = True
    both_down = False
    sec_chg = (kr.get("sec") or {}).get("chg")
    hy_chg = (kr.get("hynix") or {}).get("chg")
    if isinstance(sec_chg, float) and isinstance(hy_chg, float) and sec_chg <= -3 and hy_chg <= -3:
        both_down = True
    if miss or pr.get("hbm_weak") or both_down:
        return "WATCH"
    return "HOLD"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(
        STATE_PATH,
        {
            "ah_notified": False,
            "session_notified": False,
            "fail_notified": False,
            "empty_notified": False,
            "last_key": None,
            "updated_at": None,
        },
    )
    in_year = now.year == int(cfg["year_guard"])
    in_window = cfg["window_start"] <= today <= cfg["window_end"]
    print(f"{stamp} mode={mode} window={in_window}")
    if mode not in {"force", "dry"} and (not in_year or not in_window):
        print("outside 2026 window; silent")
        return 0

    slot = "am" if now.hour < 12 else "pm"
    phase = "ah" if slot == "am" else "session"
    if today == "2026-10-02":
        phase = "retry"

    pr = {"published": False, "rev_bn": None, "eps": None, "guide_rev_bn": None, "hbm": "조회 전", "hbm_weak": False}
    ir_ok = False
    ir_err = ""
    page_text = ""
    for url in (cfg.get("pr_guess"), cfg.get("ir_gnw"), cfg["ir_news"], cfg["ir_home"]):
        if not url:
            continue
        try:
            page_text = strip_html(fetch(url))
            parsed = parse_pr(page_text)
            ir_ok = True
            if parsed.get("published") or parsed.get("rev_bn") or parsed.get("eps"):
                pr = parsed
                break
            pr = parsed
        except Exception as exc:
            ir_err = str(exc)
            print(f"ir fail {url}: {exc}")

    quotes = {}
    q_err = ""
    try:
        quotes["mu"] = yahoo_quote(cfg["tickers"]["mu"])
        quotes["sec"] = yahoo_quote(cfg["tickers"]["sec"])
        quotes["hynix"] = yahoo_quote(cfg["tickers"]["hynix"])
    except Exception as exc:
        q_err = str(exc)
        print(f"yahoo fail: {exc}")

    if mode != "dry" and not ir_ok and not quotes:
        if not state.get("fail_notified"):
            emit(
                cfg,
                "[SEC] 마이크론 조회 실패 | WATCH",
                [
                    f"{stamp} | PORT | 마이크론 FY26 4Q",
                    "이게 뭐 알림: 마이크론 실적 원문과 삼성·하이닉스 반응만 표로 봅니다. 매수 신호가 아닙니다.",
                    f"지금 상태: IR·시세 조회 실패. {ir_err or q_err or '출처 없음'}. 숫자를 추정하지 않음.",
                    "지금 할 일: 다음 기존 슬롯(09:00 또는 15:20)까지 대기. 지금은 HOLD도 아님, WATCH.",
                    "나중에: 원문이 열리면 한 번만 표를 보냅니다. 만료 2026-10-02.",
                    "익절/줄일 때: 실패 알림으로 포지션을 바꾸지 마세요.",
                    "하지 말 것: 조회 실패를 호재로 읽기. 추격·신용·미수.",
                ],
                4,
            )
            state["fail_notified"] = True
        save_state(state, now)
        return 0

    verdict = decide(pr, quotes)
    mu = quotes.get("mu") or {}
    sec = quotes.get("sec") or {}
    hy = quotes.get("hynix") or {}
    table = (
        "표 | 매출 "
        f"{fmt_n(pr.get('rev_bn'), 'B')} / 컨센 {cfg['cons_rev_bn']}B ({vs_cons(pr.get('rev_bn'), cfg['cons_rev_bn'])}) | "
        f"EPS {fmt_n(pr.get('eps'))} / 컨센 {cfg['cons_eps']} ({vs_cons(pr.get('eps'), cfg['cons_eps'])}) | "
        f"HBM {pr.get('hbm')} | "
        f"MU 종가 {fmt_n(mu.get('px'))} 장후 {fmt_n(mu.get('post'))} ({fmt_pct(mu.get('ah'))}) | "
        f"삼성 {fmt_n(sec.get('px'))} ({fmt_pct(sec.get('chg'))}) | "
        f"닉스 {fmt_n(hy.get('px'))} ({fmt_pct(hy.get('chg'))})"
    )

    if not pr.get("published") and today < cfg["window_end"] and mode not in {"force", "dry"}:
        print("not published yet; silent")
        save_state(state, now)
        return 0

    if not pr.get("published") and today == cfg["window_end"] and state.get("empty_notified") and mode != "force":
        print("empty already sent; silent")
        return 0

    flag = "session_notified" if phase == "session" else "ah_notified"
    if phase == "retry":
        flag = "ah_notified" if not state.get("ah_notified") else "session_notified"
    if state.get(flag) and mode != "force":
        print(f"{flag} already sent; silent")
        return 0

    if pr.get("published"):
        title = f"[SEC] 마이크론 {verdict} | 삼전닉스 반응만"
    else:
        title = "[SEC] 마이크론 발표문 없음 | WATCH"
        verdict = "WATCH"

    action = "숫자와 장중 반응만 기록. 추격하지 마세요."
    if verdict == "HOLD":
        action = "컨센 하회·HBM 약화·국내 동반 -3%가 아니면 HOLD. 추가매수 금지."
    else:
        action = "하회 또는 HBM 약화 또는 국내 동반 -3%면 WATCH. 추격 매수 금지."

    lines = [
        f"{stamp} | PORT | {cfg['event_label']} | {phase}",
        "이게 뭐 알림: 마이크론 4Q 매출·EPS·HBM 코멘트와 삼성전자·SK하이닉스 시간외/당일 반응만 봅니다.",
        f"지금 상태: {verdict}. {table}",
        f"지금 할 일: {action}",
        "나중에: 같은 결과는 다시 안 보냄. 15:20에 국내 종가만 한 번 더 볼 수 있음. 만료 2026-10-02.",
        "익절/줄일 때: 이 알림은 HOLD/WATCH만. 매도 신호가 아닙니다.",
        f"하지 말 것: 컨센 {cfg['cons_rev_bn']}B/{cfg['cons_eps']}를 확정 실적으로 쓰기. 조회 실패를 호재로 읽기. 추격·신용·미수. 투자 권유 아님.",
        f"출처: {cfg['ir_news']}",
        f"조회: IR {'OK' if ir_ok else '실패'} / 시세 {'OK' if quotes else '실패'} {ir_err or q_err}",
    ]

    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("dry-run; no ntfy")
        save_state(state, now)
        return 0

    emit(cfg, title, lines, 4)
    state[flag] = True
    if not pr.get("published"):
        state["empty_notified"] = True
    state["last_key"] = json.dumps({"rev": pr.get("rev_bn"), "eps": pr.get("eps"), "verdict": verdict}, sort_keys=True)
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
