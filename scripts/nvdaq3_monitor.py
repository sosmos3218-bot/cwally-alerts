#!/usr/bin/env python3
"""NVIDIA FY2027 Q3 one-shot checklist. Existing dispatcher slots only."""
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
CFG_PATH = ROOT / "configs" / "nvdaq3.json"
STATE_PATH = ROOT / "state" / "nvdaq3.json"
UA = {"User-Agent": "cwally-alerts research", "Accept": "text/html,application/json"}


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


def num(raw: str | None):
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def sec_release_url() -> str | None:
    raw = fetch("https://data.sec.gov/submissions/CIK0001045810.json")
    data = json.loads(raw)
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    for form, day, acc, doc in zip(forms, dates, accessions, docs):
        if form not in {"8-K", "8-K/A"} or day < "2026-11-16":
            continue
        acc_nodash = acc.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/1045810/{acc_nodash}/{doc}"
    return None


def parse_text(text: str) -> dict:
    q3 = bool(re.search(r"third quarter of fiscal 2027|Q3 fiscal 2027|third-quarter fiscal 2027", text, re.I))
    actual = bool(re.search(r"revenue of \$|reported revenue|financial results", text, re.I))
    published = bool(q3 and actual)
    rev = None
    gm = None
    rubin = None
    q4_rev = None
    q4_gm = None
    if published:
        m = re.search(r"revenue of \$([0-9]+(?:\.[0-9]+)?)\s*billion", text, re.I)
        rev = num(m.group(1)) if m else None
        m = re.search(r"gross margins were both ([0-9]+(?:\.[0-9]+)?)%", text, re.I)
        if not m:
            m = re.search(r"gross margin(?:s)? (?:was|were) ([0-9]+(?:\.[0-9]+)?)%", text, re.I)
        gm = num(m.group(1)) if m else None
        m = re.search(r"Vera Rubin[^.]{0,80}?([0-9]+(?:\.[0-9]+)?)\s*% of data[- ]center", text, re.I)
        rubin = num(m.group(1)) if m else None
        outlook = ""
        m = re.search(r"outlook for the fourth quarter(.{0,500})", text, re.I)
        if m:
            outlook = m.group(1)
            rm = re.search(r"\$([0-9]+(?:\.[0-9]+)?)\s*billion", outlook)
            q4_rev = num(rm.group(1)) if rm else None
            gm_m = re.search(r"([0-9]+(?:\.[0-9]+)?)%", outlook)
            q4_gm = num(gm_m.group(1)) if gm_m else None
    china_zero = bool(re.search(r"not assuming any Data Center compute revenue from China|no Data Center compute revenue from China", text, re.I))
    china_hit = bool(re.search(r"China.{0,40}(write-down|inventory charge|license revoked)|export control.{0,40}(supply|HBM)", text, re.I))
    rubin_delay = bool(re.search(r"Vera Rubin.{0,40}(delay|pushed|slipped)", text, re.I))
    return {
        "published": bool(published and rev is not None),
        "rev": rev,
        "gm": gm,
        "rubin": rubin,
        "q4_rev": q4_rev,
        "q4_gm": q4_gm,
        "china_zero": china_zero,
        "china_hit": china_hit,
        "rubin_delay": rubin_delay,
    }


def mark(value, lo, mid, down) -> str:
    if value is None:
        return "확인불가"
    if value < down:
        return "하향"
    if value < mid:
        return "중립"
    if value >= lo:
        return "통과"
    return "중립"


def judge(p: dict, b: dict) -> dict:
    rev = mark(p.get("rev"), b["revenue_mid_b"], b["revenue_mid_b"], b["revenue_lo_b"])
    if p.get("rev") is not None and p["rev"] >= b["revenue_lo_b"] and p["rev"] < b["revenue_mid_b"]:
        rev = "중립"
    elif p.get("rev") is not None and p["rev"] >= b["revenue_mid_b"]:
        rev = "통과"
    elif p.get("rev") is not None and p["rev"] < b["revenue_lo_b"]:
        rev = "하향"
    gm = "확인불가"
    if p.get("gm") is not None:
        if p["gm"] < b["gm_down"]:
            gm = "하향"
        elif p["gm"] < b["gm_lo"]:
            gm = "중립"
        else:
            gm = "통과"
    rubin = "확인불가"
    if p.get("rubin_delay"):
        rubin = "하향"
    elif p.get("rubin") is not None:
        if 15 <= p["rubin"] <= 25 and gm != "하향":
            rubin = "통과"
        elif p["rubin"] < 10:
            rubin = "하향"
        elif gm == "하향":
            rubin = "하향"
        else:
            rubin = "중립"
    china = "통과" if p.get("china_zero") and not p.get("china_hit") else "확인불가"
    if p.get("china_hit"):
        china = "하향"
    q4 = "확인불가"
    if p.get("q4_rev") is not None and p.get("rev") is not None:
        if p["q4_rev"] < p["rev"] or (p.get("q4_gm") is not None and p["q4_gm"] < b["q4_gm_floor"]):
            q4 = "하향"
        elif p.get("q4_gm") is None or p["q4_gm"] >= b["q4_gm_floor"]:
            q4 = "통과"
    downs = sum(1 for x in (rev, gm, q4) if x == "하향")
    thesis = "낮춤" if downs >= 2 else "유지"
    return {"rev": rev, "gm": gm, "rubin": rubin, "china": china, "q4": q4, "thesis": thesis, "downs": downs}


def fmt(v, suffix=""):
    return f"{v:.1f}{suffix}" if isinstance(v, float) else "확인불가"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(STATE_PATH, {"result_notified": False, "empty_notified": False, "fail_notified": False, "updated_at": None})
    in_year = now.year == int(cfg["year_guard"])
    in_window = cfg["window_start"] <= today <= cfg["window_end"]
    print(f"{stamp} mode={mode} window={in_window}")
    if mode not in {"force", "dry"} and (not in_year or not in_window):
        print("outside 2026 window; silent")
        return 0
    text = ""
    ok = False
    err = ""
    urls = list(cfg["sources"])
    try:
        extra = sec_release_url()
        if extra:
            urls.append(extra)
    except Exception as exc:
        err = str(exc)
        print(f"sec index fail: {exc}")
    parsed = {"published": False}
    for url in urls:
        try:
            raw = fetch(url)
            ok = True
            if url.endswith(".json"):
                continue
            got = parse_text(strip_html(raw))
            if got.get("published"):
                parsed = got
                break
        except Exception as exc:
            err = str(exc)
            print(f"fetch fail {url}: {exc}")
    if mode != "dry" and not ok:
        if not state.get("fail_notified"):
            emit(cfg, "[SMH] 엔비디아 3Q 조회 실패 | 판정 유보", [
                f"{stamp} | PORT | {cfg['event_label']}",
                "이게 뭐 알림: 엔비디아 FY2027 3분기 매출, 매출총이익률, Rubin 비중, 중국 매출, 4분기 가이던스를 통과·중립·하향으로 판정합니다.",
                f"지금 상태: 조회 실패. {err or '출처 없음'}. 숫자를 추정하지 않음.",
                "지금 할 일: 다음 슬롯까지 대기. 매수·매도 없음.",
                "나중에: 원문이 열리면 한 번만 보냄. 마지막 2026-11-27 07:10.",
                "익절/줄일 때: 실패 알림으로 포지션을 바꾸지 마세요.",
                "하지 말 것: 조회 실패를 호재나 악재로 읽기. 추격, 신용, 미수.",
            ], 4)
            state["fail_notified"] = True
        save_state(state, now)
        return 0
    last = today >= cfg["window_end"]
    if not parsed.get("published") and not last and mode not in {"force", "dry"}:
        print("not published yet; silent")
        save_state(state, now)
        return 0
    if parsed.get("published") and state.get("result_notified") and mode != "force":
        print("result already sent; silent")
        return 0
    if not parsed.get("published") and state.get("empty_notified") and mode != "force":
        print("empty already sent; silent")
        return 0
    b = cfg["baselines"]
    j = judge(parsed, b) if parsed.get("published") else None
    if j and j["thesis"] == "낮춤":
        title = "[SMH] 엔비디아 3Q 기준 미달 | 보유 논거 낮춤"
        todo = "신규 매수 중단. 보유 논거는 낮춤. 원문 확인 전 전량 매도는 하지 마세요."
    elif j:
        title = "[SMH] 엔비디아 3Q 체크 | 보유 논거 유지"
        todo = "보유 논거 유지. 상단 초과만으로 추가매수하지 마세요."
    else:
        title = "[SMH] 엔비디아 3Q 아직 없음 | 판정 유보"
        todo = "공식 실적이 안 보입니다. 매수·매도 없음."
        j = {"rev": "확인불가", "gm": "확인불가", "rubin": "확인불가", "china": "확인불가", "q4": "확인불가", "thesis": "유보", "downs": 0}
    lines = [
        f"{stamp} | PORT | {cfg['event_label']} | {j['thesis']}",
        "이게 뭐 알림: FY2027 3분기 매출, 매출총이익률, Rubin 비중, 중국 매출, 4분기 가이던스를 통과·중립·하향으로 나누고 보유 논거를 유지 또는 낮춤으로 결론냅니다.",
        (
            f"지금 상태: 보유 논거 {j['thesis']}. 매출 {j['rev']} {fmt(parsed.get('rev'), ' billion')}. "
            f"매출총이익률 {j['gm']} {fmt(parsed.get('gm'), '%')}. Rubin {j['rubin']} {fmt(parsed.get('rubin'), '%')}. "
            f"중국 {j['china']}. 4분기 가이던스 {j['q4']} 매출 {fmt(parsed.get('q4_rev'), ' billion')} 마진 {fmt(parsed.get('q4_gm'), '%')}. "
            f"하향 {j['downs']}개. 기준은 매출 1,080억 ±2%, 마진 74% ±0.5%p, 중국 0."
        ),
        f"지금 할 일: {todo}",
        "나중에: 같은 결과는 다시 안 보냄. 창은 2026-11-27 07:10까지. 그 뒤 침묵.",
        "익절/줄일 때: 매출 하단 미달, 마진 73% 미만, 4분기 역성장 중 두 개 이상이면 논거를 낮춤. 하나만이면 유지.",
        "하지 말 것: 미발표를 악재로 읽기. 콜 전언 20%를 공식 기준으로 쓰기. 추격, 신용, 미수. 투자 권유 아님.",
        f"출처: {cfg['sources'][0]}",
        f"조회: {'OK' if ok and not err else ('일부 실패: ' + err if ok else '실패: ' + err)}",
    ]
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("dry-run; no ntfy")
        return 0
    emit(cfg, title, lines, 4)
    if parsed.get("published"):
        state["result_notified"] = True
    else:
        state["empty_notified"] = True
    state["last_key"] = j["thesis"]
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
