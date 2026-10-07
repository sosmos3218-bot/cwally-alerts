#!/usr/bin/env python3
"""0144M0 follow-up: JOBY/ACHR 8-K only when certification language appears."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "uamcert.json"
STATE_PATH = ROOT / "state" / "uamcert.json"
UA = {"User-Agent": "cwally-alerts uamcert research monitor research@example.com"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return json.loads(json.dumps(fallback))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.loads(json.dumps(fallback))
    out = json.loads(json.dumps(fallback))
    out.update(data)
    return out


def fetch(url: str, limit: int = 800_000) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read(limit).decode("utf-8", errors="ignore")


def visible(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def new_filings(cik: str, forms: set[str], seen: set[str]) -> tuple[list[dict], str | None]:
    raw = fetch(f"https://data.sec.gov/submissions/CIK{cik}.json")
    recent = (json.loads(raw).get("filings") or {}).get("recent") or {}
    out = []
    for form, day, acc, doc in zip(
        recent.get("form") or [],
        recent.get("filingDate") or [],
        recent.get("accessionNumber") or [],
        recent.get("primaryDocument") or [],
    ):
        if form not in forms or acc in seen:
            continue
        out.append({"form": form, "date": day, "acc": acc, "doc": doc})
    out.sort(key=lambda row: row["date"])
    return out, None


def doc_url(cik: str, acc: str, doc: str) -> str:
    cik_n = str(int(cik))
    acc_n = acc.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_n}/{acc_n}/{doc}"


def hits(text: str, keywords: list[str]) -> list[str]:
    low = text.lower()
    return [kw for kw in keywords if kw in low]


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | 0144M0 | JOBY/ACHR 인증 공시",
            "이게 뭐 알림: 0144M0 상위 개발사의 새 8-K에 인증 문구가 있을 때만 보내는 알림입니다. 매수 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("UAMCERT_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(
        STATE_PATH,
        {"updated_at": None, "seen": {}, "fetch_fail_date": None, "alerted": []},
    )
    now = datetime.now(KST)
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if mode not in {"force", "dry"} and (today < cfg["valid_from"] or today > cfg["valid_until"]):
        print(f"outside {cfg['valid_from']}..{cfg['valid_until']}; silent")
        return 0

    forms = set(cfg["forms"])
    keywords = cfg["keywords"]
    alerts: list[tuple[str, str, int]] = []
    seen = state.get("seen") or {}
    fail = False
    for issuer in cfg["issuers"]:
        ticker = issuer["ticker"]
        known = set(seen.get(ticker) or [])
        known.add(issuer["baseline_acc"])
        try:
            rows, _ = new_filings(issuer["cik"], forms, known)
        except Exception as exc:
            print(f"{ticker} submissions fail: {exc}")
            fail = True
            continue
        for row in rows:
            known.add(row["acc"])
            text = ""
            try:
                text = visible(fetch(doc_url(issuer["cik"], row["acc"], row["doc"])))
            except Exception as exc:
                print(f"{ticker} doc fail {row['acc']}: {exc}")
                fail = True
                continue
            matched = hits(text, keywords)
            key = f"{ticker}@{row['acc']}"
            if matched and key not in set(state.get("alerted") or []):
                url = doc_url(issuer["cik"], row["acc"], row["doc"])
                alerts.append(
                    (
                        f"[0144M0] {ticker} 인증 공시 | 재평가",
                        body(
                            stamp,
                            f"{issuer['name']} 새 {row['form']} {row['date']}. 인증 문구: {', '.join(matched)}. 원문 {url}",
                            "0144M0 핵심배분 금지는 유지하세요. 원문에서 형식증명 단계와 상업 운항 일정이 문서로 확정됐는지만 확인하세요.",
                            "일정 확정과 희석 없는 자금조달이 같이 나와야 위성 한도를 다시 봅니다. 문구만으로는 부족합니다.",
                            "인증 지연이나 추가 유상증자 문구가 있으면 위성 비중도 줄이세요.",
                            "8-K 제목만 보고 매수. 신용, 미수. 인증 진행 문구를 운항 개시로 단정.",
                        ),
                        4,
                    )
                )
                state.setdefault("alerted", []).append(key)
            else:
                print(f"{ticker} {row['date']} {row['acc']} cert={'yes' if matched else 'no'}")
        seen[ticker] = sorted(known)
    state["seen"] = seen

    if fail and state.get("fetch_fail_date") != today:
        alerts.append(
            (
                "[0144M0] 인증 공시 조회 실패 | 조건 미확인",
                body(
                    stamp,
                    "JOBY 또는 ACHR 8-K 조회에 실패했습니다. 인증 공시가 없다는 뜻이 아닙니다.",
                    "SEC EDGAR에서 JOBY, ACHR 최신 8-K를 직접 보세요.",
                    "다음 평일 09:00 KST에 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 인증 없음이나 매수 신호로 읽기.",
                ),
                3,
            )
        )
        state["fetch_fail_date"] = today

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["warning"], server)
    if not alerts:
        print("no alert")

    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
