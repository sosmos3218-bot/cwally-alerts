#!/usr/bin/env python3
"""SEMCRIT: 영상 기반 단기 반도체 리스크 관찰. 매매 신호 아님."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "configs" / "semicrit.json").read_text(encoding="utf-8"))

def year_ok() -> bool:
    return datetime.now(KST).year == CFG.get("year_guard", 2026)

def fetch_foreign(code: str) -> str:
    try:
        url = f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize=3&page=1"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
        items = data.get("trendList") or data.get("items") or []
        if not items:
            return "데이터 없음"
        last = items[0]
        net = last.get("foreignNetBuyAmount") or last.get("foreignerNetBuy") or "?"
        return f"{net}"
    except Exception as e:
        return f"조회 실패: {e}"

def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if not year_ok():
        print("year guard: silent")
        return 0
    sk = fetch_foreign("000660")
    sec = fetch_foreign("005930")
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    title = "[SEMCRIT] 단기 반도체 리스크 관찰 | 매매 신호 아님"
    body = (
        "1. 이게 뭐 알림: 오픈AI 매출 혼선, 순환금융, 솔리다임, 외국인 매도 관련 단기 관찰\n"
        "2. 지금 결과/상태: SK하이닉스 외국인 순매수 " + sk + " / 삼성전자 " + sec + " (완료일 기준)\n"
        "3. 지금 할 일: 외국인 순매도 지속 시 보수적 대기, 순매수 전환 확인 후 재검토\n"
        "4. 나중에: 솔리다임 상장 관련 주주보호 공시, OpenAI 실제 매출 추이 확인\n"
        "5. 익절/줄일 때: 해당 없음 (관찰 전용)\n"
        "6. 하지 말 것: 이 알림만으로 매수/매도 결정하지 말 것. 1차 공시·실적 확인 필수"
    )
    if mode == "dry":
        print("DRY RUN")
        print(title)
        print(body)
        return 0
    from scripts.ntfy_util import publish
    publish("cwally-port", title, body)
    print(f"sent {now}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
