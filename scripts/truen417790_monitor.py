#!/usr/bin/env python3
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import publish

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "truen417790.json"

def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "dry":
        print("DRY RUN: would send [TRUEN] status summary")
        return 0
    cfg = json.loads((ROOT / "configs" / "truen417790.json").read_text(encoding="utf-8"))
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [
        f"{now} | TRUEN | \ud2b8\ub8e8\uc5d4(417790) \uc810\uac80",
        "\uc774\uac8c \ubb50 \uc54c\ub9bc: \ud2b8\ub8e8\uc5d4 3\ubd84\uae30 \uc2e4\uc801 \ud6c4 \ud574\uc678\u00b7IoT \ube44\uc911, \uc790\uc0ac\uc8fc\u00b7\ubc30\ub2f9 \uc815\ucc45, \uc21c\ud604\uae08 \ud65c\uc6a9 \ubbfc\uac10\ub3c4 \uc694\uc57d \uc54c\ub9bc\uc785\ub2c8\ub2e4.",
        "\uc9c0\uae08 \uc0c1\ud0dc: \ud574\uc678(TA\uc2dc\ub9ac\uc988)\ub294 \uc0d8\ud50c \uacf5\uae09 \ub2e8\uacc4(10\uc5ec\uac1c\uad6d), \ubcf8 \uc218\uc8fc \ubbf8\ud655\uc778. \uc774\uae00\ub8e8 IoT \uc218\ucd9c +48% \uc131\uc7a5(\uc77c\ubcf8 \uacac\uc778). \uacf5\uacf5\uc740 \ud55c\uad6d\uac00\uc2a4\uacf5\uc0ac \uc2e4\uc99d(10/12) \ub808\ud37c\ub7f0\uc2a4. \uc21c\ud604\uae08 ~700\uc5b5, PER ~6\ubc30, \uc774\ubbf8 ~60\uc5b5 \uc18c\uac01 \uc644\ub8cc.",
        "\uc9c0\uae08 \ud560 \uc77c: 3\ubd84\uae30 \uc2e4\uc801 \uacf5\uc2dc\uc5d0\uc11c \ud574\uc678\u00b7IoT \ub9e4\ucd9c \ube44\uc911 \ubcc0\ud654\uc640 \ucd94\uac00 \uc790\uc0ac\uc8fc \uc18c\uac01/\ubc30\ub2f9 \uc5b8\uae09\uc744 \ud655\uc778\ud558\uc138\uc694. \ud604\uc7ac \ucd94\uac00 \uc18c\uac01(50\uc5b5 \uac00\uc815) \uc2dc EPS +7% \uc218\uc900.",
        "\ub098\uc911\uc5d0: \uc0d8\ud50c\u2192\uc591\uc0b0 \uc804\ud658, \uc544\ub9c8\uc874 \uc601\uad6d \ub9e4\ucd9c, \uac00\uc2a4\uacf5\uc0ac \uc804\uc0ac \ud655\ub300 \uc5ec\ubd80 \ud655\uc778. \uc131\uc7a5 \ud22c\uc790 vs \uc18c\uac01 \uade0\ud615 \uc720\uc9c0.",
        "\uc775\uc808/\uc904\uc77c \ub54c: \uc21c\ud604\uae08 \ud504\ub9ac\ubbf8\uc5c4 \ud574\uc18c \uc2dc \ub610\ub294 \uc131\uc7a5 \uc9c0\uc5f0 \ud655\uc778 \uc2dc \ube44\uc911 \uc870\uc808. \ud604\uc7ac \uc800\ud3c9\uac00 \uad6c\uac04.",
        "\ud558\uc9c0 \ub9d0 \uac83: \ud574\uc678 \uc218\uc8fc \ubbf8\ud655\uc778 \uc0c1\ud0dc\uc5d0\uc11c \ucd94\uaca9\ub9e4\uc218, \uacfc\ub3c4\ud55c \uc131\uc7a5 \ud22c\uc790 \uae30\ub300, \uc2e0\uc6a9/\ubbf8\uc218.",
    ]
    title = "[TRUEN] 3Q \ub300\uae30 | \ud574\uc678\u00b7\ud658\uc6d0 \uc810\uac80"
    msg = "\n".join(lines)
    print(msg)
    publish(cfg["ntfy_topic"], title, msg, priority=3)
    STATE_PATH.write_text(json.dumps({"updated_at": now, "last_sent": now}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
