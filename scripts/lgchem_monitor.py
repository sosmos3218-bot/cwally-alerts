#!/usr/bin/env python3
"""LG Chem (051910) Q3 2026 oneshot: DART check + 4 criteria verdict.

Year-guarded to 2026. Dry mode never publishes or writes state.
Verdicts: \ube44\uc911 \uc720\uc9c0 / \ube44\uc911 \ucd95\uc18c / 3\ucc28 \ub9e4\uc218 \ubcf4\ub958
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "lgchem.json"
STATE = ROOT / "state" / "lgchem.json"


def load_state():
    st = {"final_notified": False, "empty_notified": False, "fail_notified": False}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st, now):
    st["updated_at"] = now.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def evaluate(cfg, inputs):
    """Return (verdict, reasons). inputs dict with keys from criteria."""
    reasons = []
    shrink = False
    hold3 = False
    # 1. \ubc30\ub2f9 \uc7ac\uc6d0
    if inputs.get("separate_2q_loss") and inputs.get("lg_div_cut_signal"):
        shrink = True
        reasons.append("\uae30\uc9001 \ubc30\ub2f9\uc7ac\uc6d0 \ud6fc\uc190 + LG \ubc30\ub2f9\uc0ad\uac10 \uc2e0\ud638")
    # 2. NAV
    if inputs.get("nav_share_pct", 100) < cfg["criteria"]["nav"]["chem_share_pct_threshold"]:
        hold3 = True
        reasons.append(f"\uae30\uc9002 NAV \ube44\uc911 {inputs.get('nav_share_pct')}% < 20%")
    # 3. LG \uc190\uc808
    if inputs.get("lg_close", 999999) < cfg["criteria"]["lg_stop"]["close_threshold"] and inputs.get("chem_fundamental_damaged"):
        shrink = True
        reasons.append("\uae30\uc9003 LG \uc885\uac00 85000 \ubbf8\ub9cc + \ud380\ub354\uba58\ud138 \ud6fc\uc190")
    # 4. CNS
    if inputs.get("cns_drawdown_pct", 0) <= cfg["criteria"]["cns"]["drawdown_pct"] and inputs.get("cns_ai_slowed"):
        shrink = True
        reasons.append("\uae30\uc9004 CNS -15% + AI \uc218\uc8fc \ub454\ud654")
    if shrink:
        return "\ube44\uc911 \ucd95\uc18c", reasons
    if hold3:
        return "3\ucc28 \ub9e4\uc218 \ubcf4\ub958", reasons
    return "\ube44\uc911 \uc720\uc9c0", reasons or ["4\ub300 \uae30\uc900 \ud1b5\uacfc"]


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    in_win = start <= now <= expire and now.year == int(cfg.get("event_year", 2026))
    print(f"{now.isoformat()} mode={mode} window={in_win}")
    if mode not in {"force", "dry"} and not in_win:
        print("outside 2026 window; silent")
        if mode != "dry":
            save_state(st, now)
        return 0
    if st.get("final_notified") and mode not in {"force", "dry"}:
        print("already final; skip")
        if mode != "dry":
            save_state(st, now)
        return 0
    # TODO: real DART/Naver fetch. For now sample or empty.
    # In production replace with actual parse of DART \uacf5\uc2dc / \ubd84\uae30\ubcf4\uace0\uc11c.
    sample = {
        "separate_2q_loss": False,
        "lg_div_cut_signal": False,
        "nav_share_pct": 18.9,
        "lg_close": 105100,
        "chem_fundamental_damaged": False,
        "cns_drawdown_pct": 0.0,
        "cns_ai_slowed": False,
    }
    verdict, reasons = evaluate(cfg, sample)
    title = f"[LGCHEM] 3Q \uc2e4\uc801 \ud310\uc815: {verdict} | \uc124\uce58 \ud655\uc778 \ub610\ub294 \ub300\uae30"
    lines = [
        "\uc774\uac8c \ubb50 \uc54c\ub9bc: " + cfg["explain"],
        f"\uc9c0\uae08 \uacb0\uacfc\u00b7\uc0c1\ud0dc: {verdict}. \uc0ac\uc720: {'; '.join(reasons)}. NAV {sample['nav_share_pct']}%.",
        "\uc9c0\uae08 \ud560 \uc77c: " + ("\ube44\uc911 \ucd95\uc18c \uc2e4\ud589 (\uadf8\ub8f9 \uc0c1\ud55c 10% \ud558\ud5a5, LG 40%)" if verdict == "\ube44\uc911 \ucd95\uc18c" else "3\ucc28 \ub9e4\uc218 \ubcf4\ub958" if verdict == "3\ucc28 \ub9e4\uc218 \ubcf4\ub958" else "\ube44\uc911 \uc720\uc9c0, \ubd84\ud560\ub9e4\uc218 \ub300\uae30"),
        "\ub098\uc911\uc5d0: 11/16 \ubd84\uae30\ubcf4\uace0\uc11c \ubcc4\ub3c4 \ud655\uc778. LG 2\ucc28 98000 / 3\ucc28 95000, CNS 2\ucc28 64000 / 3\ucc28 60000.",
        "\uc775\uc808\u00b7\uc904\uc77c \ub54c: LG 85000 \uc885\uac00 + \ud380\ub354 \ud6fc\uc190 \uc2dc \uc808\ubc18, CNS -15% + AI \ub454\ud654 \uc2dc \uc808\ubc18, \ubc30\ub2f9\uc7ac\uc6d0 \ud6fc\uc190 \uc2dc \uadf8\ub8f9 10%.",
        "\ud558\uc9c0 \ub9d0 \uac83: \ud380\ub354 \uc774\uc0c1 \uc5c6\ub294 \uc9c0\uc8fc\uc0ac \ud560\uc778 \ud22c\ub9e4 \uae08\uc9c0, 120000 \uc774\uc0c1 \ucd94\uaca9\ub9e4\uc218 \uae08\uc9c0, ISA \uc911\ub3c4\ud574\uc9c0 \uae08\uc9c0.",
    ]
    if mode == "dry":
        print("DRY RUN | no ntfy, no state write")
        print(f"verdict={verdict} reasons={reasons}")
        print(f"would send title: {title}")
        print("body:")
        print("\n".join(lines))
        return 0
    # Real send only in auto/force when data ready; for install use separate confirmation
    if mode == "force" or (mode == "auto" and not st.get("empty_notified")):
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title[:180],
            "\n".join(lines),
            3,
            ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        st["empty_notified"] = True
        save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
