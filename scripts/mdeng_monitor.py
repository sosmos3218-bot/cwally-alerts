#!/usr/bin/env python3
"""[LDOS] VA MDE-NG (36C10X27R0001) 신규 수정/공고 알림.

평일 09:00 KST port-alerts morning 슬롯. SAM.gov 공개 검색으로 solnum=36C10X27R0001 공고를 보고
notice id + modifiedDate + modifications.count 키가 state.seen 에 없으면 1번 알림.
최종 RFP/DRAFT 는 state 에 시드되어 재알림 없음. 2027-06-30 이후는 no-op.
usage: mdeng_monitor.py <auto|dry> | selftest | confirm
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "mdeng"
TAG = "[LDOS]"
TICKER = "LDOS"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
SGS_URL = (
    "https://sam.gov/api/prod/sgs/v1/search/"
    "?index=opp&q={q}&page=0&size=25&mode=search"
)
OFFICIAL_URL = (
    "https://api.sam.gov/opportunities/v2/search"
    "?solnum={sol}&limit=25&api_key={key}"
)


def default_state() -> dict:
    return {
        "updated_at": None,
        "seeded_at": None,
        "seen": [],
        "last_response_deadline": None,
        "fail_streak": 0,
        "ops_day": None,
    }


def load_state() -> dict:
    st = default_state()
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    st.setdefault("seen", [])
    return st


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_json(url: str, headers: dict | None = None, timeout: int = 30) -> dict:
    h = {"User-Agent": UA, "Accept": "application/hal+json, application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def notice_key(n: dict) -> str:
    mod = n.get("modifications") or {}
    count = mod.get("count", 0) if isinstance(mod, dict) else 0
    return f"{n.get('_id') or n.get('noticeId')}|{n.get('modifiedDate') or n.get('postedDate')}|{count}"


def normalize_sgs(row: dict) -> dict:
    return {
        "_id": row.get("_id"),
        "title": row.get("title") or "",
        "type": row.get("type") or {},
        "publishDate": row.get("publishDate"),
        "modifiedDate": row.get("modifiedDate") or row.get("publishDate"),
        "responseDate": row.get("responseDate"),
        "responseDateActual": row.get("responseDateActual"),
        "responseTimeZone": row.get("responseTimeZone"),
        "solicitationNumber": row.get("solicitationNumber"),
        "modifications": row.get("modifications") or {"count": 0},
        "isActive": row.get("isActive"),
    }


def normalize_official(row: dict) -> dict:
    # opportunities v2 shape
    nid = row.get("noticeId") or row.get("opportunityId") or row.get("_id")
    return {
        "_id": nid,
        "title": row.get("title") or "",
        "type": {"code": row.get("type") or "", "value": row.get("type") or ""},
        "publishDate": row.get("postedDate") or row.get("publishDate"),
        "modifiedDate": row.get("modifiedDate") or row.get("postedDate"),
        "responseDate": row.get("responseDeadLine") or row.get("responseDate"),
        "responseDateActual": row.get("responseDeadLine"),
        "responseTimeZone": None,
        "solicitationNumber": row.get("solicitationNumber") or row.get("solnum"),
        "modifications": {"count": int(row.get("baseType") == "a") if False else 0},
        "isActive": row.get("active") != "No",
    }


def fetch_notices(cfg: dict) -> list[dict]:
    sol = cfg["solnum"]
    key = (os.environ.get("SAM_API_KEY") or "").strip()
    notices: list[dict] = []
    errors: list[str] = []

    if key:
        try:
            url = OFFICIAL_URL.format(sol=urllib.parse.quote(sol), key=urllib.parse.quote(key))
            data = http_json(url, headers={"Accept": "application/json"})
            for row in data.get("opportunitiesData") or []:
                n = normalize_official(row)
                if n.get("solicitationNumber") == sol and n.get("_id"):
                    notices.append(n)
            if notices:
                print(f"sam official ok: {len(notices)} notices")
                return notices
            errors.append("official returned 0 matching notices")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"official: {exc}")
            print(f"SAM official API fail (fallback to public): {exc}", file=sys.stderr)

    try:
        url = SGS_URL.format(q=urllib.parse.quote(sol))
        data = http_json(url)
        for row in (data.get("_embedded") or {}).get("results") or []:
            n = normalize_sgs(row)
            if n.get("solicitationNumber") == sol and n.get("_id"):
                notices.append(n)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"sgs: {exc}")
        raise RuntimeError("; ".join(errors) or str(exc)) from exc

    if not notices and errors:
        raise RuntimeError("; ".join(errors))
    print(f"sam public sgs ok: {len(notices)} notices")
    return notices


def parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_kst(s: str | None) -> str:
    dt = parse_utc(s)
    if not dt:
        return s or "미확인"
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def type_label(n: dict) -> str:
    t = n.get("type") or {}
    if isinstance(t, dict):
        return t.get("value") or t.get("code") or "미확인"
    return str(t) or "미확인"


def sam_link(n: dict) -> str:
    return f"https://sam.gov/opp/{n['_id']}/view"


def kind_kr(n: dict, prev_keys: set[str]) -> str:
    """Classify for title: 수정 vs 신규 공고."""
    nid = n["_id"]
    same_id = any(k.startswith(nid + "|") for k in prev_keys)
    title = (n.get("title") or "").lower()
    tval = type_label(n).lower()
    if same_id or "amend" in title or "modification" in title or "수정" in title:
        return "수정 공고"
    if "q&a" in title or "questions" in title or "qa" in title.replace(" ", ""):
        return "Q&A 공고"
    if "presolicitation" in tval or "draft" in title:
        return "사전/초안 공고"
    return "신규 공고"


def alert_message(cfg: dict, n: dict, kind: str, stamp: str, prev_deadline: str | None) -> tuple[str, str, int]:
    title = f"{TAG} MDE-NG {kind} | {TICKER} 확인"
    new_dl = n.get("responseDate") or n.get("responseDateActual")
    dl_line = fmt_kst(new_dl)
    if new_dl and prev_deadline and new_dl != prev_deadline:
        dl_note = f"응답 마감이 바뀜: 이전 {fmt_kst(prev_deadline)} -> 지금 {dl_line}"
    elif new_dl:
        dl_note = f"응답 마감: {dl_line}"
    else:
        dl_note = "응답 마감: 미확인"

    ms = cfg.get("milestones") or {}
    proposals = ms.get("proposals_et") or "2027-01-06 10:00 EST"
    proposals_kst = ms.get("proposals_kst") or "2027-01-07 00:00 KST"
    questions = ms.get("questions_due") or "2026-11-16"
    intent = ms.get("intent_due") or "2026-12-01"
    qa_week = ms.get("qa_amendment_week") or "2026-11-30 주간 질의 회신과 후속 수정 공고(예고, 아직 미발행)"
    conf_week = ms.get("preproposal_conference_week") or "2026-12-07 주간 사전제안설명회(예고, 세부 미공지)"
    body = "\n".join(
        [
            "이게 뭐 알림: VA MDE-NG 입찰(36C10X27R0001)에 새 공고 또는 수정이 올라왔다는 알림. "
            f"LDOS Health/MDE 재경쟁 점검용. 매매 신호 아님.",
            f"{stamp} | {TICKER} | Leidos Holdings | sol {cfg['solnum']}",
            f"지금 결과/상태: {kind}. 제목: {n.get('title') or '?'}. 유형: {type_label(n)}. "
            f"게시(KST): {fmt_kst(n.get('publishDate'))}. 수정(KST): {fmt_kst(n.get('modifiedDate'))}. "
            f"{dl_note}. 링크: {sam_link(n)}",
            "지금 할 일: SAM 링크를 열고 수정/첨부/Q&A 내용을 확인. Health 마진 가정(4Q high teens)과 "
            f"제안 조건이 바뀌었는지 표시해 두기. 입찰 일정(질문 {questions}, intent {intent}, "
            f"제안 {proposals}, 서울 {proposals_kst})도 같이 보기. {qa_week}, {conf_week}도 확인.",
            "나중에: 같은 공고 id 와 수정 키는 다시 안 옴. 다음 수정이 올라오면 다시 알림. "
            "2027-06-30 이후에는 자동 종료.",
            "익절/줄일 때: 미보유, 해당 없음. 포지션이 생기면 그때 기준을 적기.",
            "하지 말 것: 공고만 보고 바로 매매, 마감 전 추측으로 비중 확대, 이 알림을 낙찰 신호로 읽기.",
        ]
    )
    return title, body, 4


def ops_message(stamp: str, err: str, streak: int) -> tuple[str, str, int]:
    title = f"{TAG} MDE-NG 조회 실패 | 판정 보류"
    body = "\n".join(
        [
            "이게 뭐 알림: VA MDE-NG SAM.gov 조회가 연속 실패했다는 운영 알림. 매매 신호 아님.",
            f"{stamp} | {TICKER} | Leidos Holdings",
            f"지금 결과/상태: 연속 실패 {streak}회. 오류: {err[:300]}",
            "지금 할 일: sam.gov 에서 36C10X27R0001 을 직접 검색해 새 수정이 없는지 확인.",
            "나중에: 다음 09:00 재시도. 키가 필요해지면 SAM_API_KEY 시크릿을 검토.",
            "익절/줄일 때: 해당 없음.",
            "하지 말 것: 조회 실패를 공고 없음으로 단정.",
        ]
    )
    return title, body, 3


def evaluate(cfg: dict, st: dict, notices: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    seen = set(st.get("seen") or [])
    prev_deadline = st.get("last_response_deadline")
    out: list[tuple[str, str, int]] = []
    # newest first
    ordered = sorted(
        notices,
        key=lambda n: n.get("modifiedDate") or n.get("publishDate") or "",
        reverse=True,
    )
    new_keys: list[str] = []
    for n in ordered:
        key = notice_key(n)
        if key in seen:
            continue
        kind = kind_kr(n, seen)
        out.append(alert_message(cfg, n, kind, stamp, prev_deadline))
        new_keys.append(key)
        seen.add(key)
        if n.get("responseDate"):
            st["last_response_deadline"] = n["responseDate"]
    st["seen"] = sorted(seen)
    st["_new_keys"] = new_keys  # debug for selftest/dry
    return out


def ended(cfg: dict, now: datetime) -> bool:
    # hard no-op after 2027-06-30 (calendar date in KST)
    noop = (cfg.get("rules") or {}).get("noop_after_date") or "2027-06-30"
    if now.astimezone(KST).date().isoformat() > noop:
        return True
    ends = cfg.get("ends_kst")
    if ends and now > datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        return True
    return False


def run(mode: str, now: datetime, cfg: dict, io: dict | None = None) -> tuple[list, dict]:
    dry = mode == "dry"
    st = (io or {}).get("state")
    if st is None:
        st = load_state()
    else:
        # copy so caller can reuse
        st = dict(st)
        st["seen"] = list(st.get("seen") or [])

    if ended(cfg, now):
        print(f"{NAME} ended after {(cfg.get('rules') or {}).get('noop_after_date')}; silent")
        return [], st

    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    try:
        if io and "notices" in io:
            notices = io["notices"]() if callable(io["notices"]) else io["notices"]
        else:
            notices = fetch_notices(cfg)
        st["fail_streak"] = 0
    except Exception as exc:  # noqa: BLE001
        print(f"mdeng fetch fail (quiet): {exc}", file=sys.stderr)
        st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
        need = int((cfg.get("rules") or {}).get("fail_ops_after") or 3)
        day = now.strftime("%Y-%m-%d")
        out = []
        if st["fail_streak"] >= need and st.get("ops_day") != day:
            out = [ops_message(stamp, str(exc), st["fail_streak"])]
            st["ops_day"] = day
        if not dry and "state" not in (io or {}):
            for t, m, pri in out:
                publish(
                    resolve_topic("ops", cfg.get("ntfy_ops_topic")),
                    t,
                    m,
                    pri,
                    ["warning"],
                    resolve_server(cfg.get("ntfy_server")),
                    config_name=NAME,
                )
            st["updated_at"] = now.isoformat()
            save_state(st)
        elif dry:
            for t, m, pri in out:
                print(f"TITLE: {t}\nPRIORITY: {pri}\n{m}\n")
            print("DRY RUN: no publish, state not written")
        return out, st

    alerts = evaluate(cfg, st, notices, stamp)
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for t, m, pri in alerts:
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
        if not dry:
            publish(topic, t, m, pri, ["mega"], server, config_name=NAME)
    if not alerts:
        print("no alert")
    st["updated_at"] = now.isoformat()
    if dry:
        print("DRY RUN: no publish, state not written")
    elif "state" not in (io or {}):
        save_state(st)
    return alerts, st


def selftest(cfg: dict) -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got={got!r} want={want!r}")

    fixture = json.loads((FX / "search.json").read_text(encoding="utf-8"))
    check("fixture has final RFP + draft", len(fixture), 2)
    check("fixture solnum only", {n["solicitationNumber"] for n in fixture}, {"36C10X27R0001"})

    seeded = [notice_key(n) for n in fixture]
    st = default_state()
    st["seen"] = list(seeded)
    st["last_response_deadline"] = fixture[0]["responseDate"]

    out, st2 = run(
        "dry",
        datetime(2026, 11, 6, 9, 0, tzinfo=KST),
        cfg,
        {"state": st, "notices": fixture},
    )
    check("seeded silent", out, [])
    check("seen unchanged size", len(st2["seen"]), 2)

    # new amendment on final RFP (same id, new modifiedDate / mod count)
    amended = dict(fixture[0])
    amended["modifiedDate"] = "2026-11-10T15:00:00+00:00"
    amended["modifications"] = {"count": 1}
    amended["title"] = "Q403-RFP Medical Disability Examinations-Next Generation (MDE-NG) Amendment 0001"
    amended["responseDate"] = "2026-12-20T15:00:00+00:00"
    out, st3 = run(
        "dry",
        datetime(2026, 11, 11, 9, 0, tzinfo=KST),
        cfg,
        {"state": st2, "notices": [amended, fixture[1]]},
    )
    check("amendment fires once", len(out), 1)
    check("title has LDOS and MDE-NG", ("[LDOS]" in out[0][0] and "MDE-NG" in out[0][0] and "수정" in out[0][0]), True)
    check("body has ticker and link and KST", all(
        x in out[0][1] for x in ("LDOS", "sam.gov/opp/", "KST", "유형:", amended["title"][:20])
    ), True)
    check("deadline change noted", "응답 마감이 바뀜" in out[0][1], True)
    check("watch wording in body", all(
        x in out[0][1] for x in (
            "2027-01-06 10:00 EST",
            "2027-01-07 00:00 KST",
            "2026-11-30",
            "2026-12-07",
            "질의 회신",
            "후속 수정 공고",
            "사전제안설명회",
        )
    ), True)
    check("no middle dot / em-dash", any(ch in out[0][0] + out[0][1] for ch in "\u00b7\u2014"), False)
    parts = [ln.split(":")[0] for ln in out[0][1].split("\n")]
    check("six labels", parts[0:1] + parts[2:], ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])

    out2, st4 = run(
        "dry",
        datetime(2026, 11, 11, 9, 5, tzinfo=KST),
        cfg,
        {"state": st3, "notices": [amended, fixture[1]]},
    )
    check("same amendment not repeated", out2, [])

    # brand-new Q&A notice
    qa = {
        "_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "title": "Q403-MDE-NG Questions and Answers",
        "type": {"code": "s", "value": "Special Notice"},
        "publishDate": "2026-11-12T18:00:00+00:00",
        "modifiedDate": "2026-11-12T18:00:00+00:00",
        "responseDate": amended["responseDate"],
        "solicitationNumber": "36C10X27R0001",
        "modifications": {"count": 0},
    }
    out3, _ = run(
        "dry",
        datetime(2026, 11, 13, 9, 0, tzinfo=KST),
        cfg,
        {"state": st4, "notices": [amended, fixture[1], qa]},
    )
    check("Q&A new notice fires", len(out3), 1)
    check("Q&A title kind", "Q&A" in out3[0][0] or "신규" in out3[0][0], True)

    # unrelated solnum ignored by fetch filter simulation
    foreign = dict(qa)
    foreign["_id"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    foreign["solicitationNumber"] = "36C10X26R0042"
    filtered = [n for n in [amended, fixture[1], foreign] if n["solicitationNumber"] == cfg["solnum"]]
    check("foreign solnum dropped", [n["_id"] for n in filtered], [amended["_id"], fixture[1]["_id"]])

    # ended no-op
    out_e, _ = run(
        "dry",
        datetime(2027, 7, 1, 9, 0, tzinfo=KST),
        cfg,
        {"state": default_state(), "notices": [qa]},
    )
    check("after 2027-06-30 no-op", out_e, [])

    # quiet fail then ops on 3rd
    bad = {"state": default_state(), "notices": lambda: (_ for _ in ()).throw(OSError("timeout"))}
    streaks = []
    for d in (6, 7, 8):
        o, bad["state"] = run("dry", datetime(2026, 11, d, 9, 0, tzinfo=KST), cfg, bad)
        streaks.append((bad["state"]["fail_streak"], len(o)))
    check("fail quiet then ops on 3rd", streaks, [(1, 0), (2, 0), (3, 1)])
    o3, _ = run("dry", datetime(2026, 11, 8, 10, 0, tzinfo=KST), cfg, bad)
    check("ops once per day", o3, [])

    check("ends_kst", cfg["ends_kst"], "2027-06-30 23:59")
    check("proposals milestone EST", (cfg.get("milestones") or {}).get("proposals_et"), "2027-01-06 10:00 EST")
    check("topic port", cfg["ntfy_topic"], "cwally-port")
    check("prefix LDOS", cfg["title_prefix"], "[LDOS]")
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def confirm(cfg: dict) -> int:
    body = "\n".join(
        [
            "이게 뭐 알림: LDOS VA MDE-NG(36C10X27R0001) 수정 공고 알림 설치 확인입니다. 매매 신호 아님.",
            f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')} | {TICKER} | Leidos Holdings",
            "지금 결과/상태: 최종 RFP, DRAFT, mod 1-2, Amendment 0001 를 seen 에 시드함. "
            "지금은 발송 대상 없음. 새 수정/Q&A/버전만 알림. 제안 마감은 2027-01-06 10:00 EST "
            "(서울 2027-01-07 00:00 KST).",
            "지금 할 일: 없음. 평일 09:00 KST morning 슬롯에서 SAM 공개 검색을 본다. "
            "2026-11-30 주간 질의 회신과 후속 수정 공고, 2026-12-07 주간 사전제안설명회 예고를 본다.",
            "나중에: 2027-06-30 이후 자동 종료.",
            "익절/줄일 때: 미보유, 해당 없음.",
            "하지 말 것: 이 설치 확인을 매매 신호로 읽기.",
        ]
    )
    title = f"{TAG} MDE-NG 설치 확인 | 매매 신호 아님"
    print(title)
    print(body)
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        body,
        3,
        ["white_check_mark"],
        resolve_server(cfg.get("ntfy_server")),
        config_name=NAME,
    )
    return 0


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    mode = args[0] if args else "auto"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode == "confirm":
        return confirm(cfg)
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    if ended(cfg, now):
        print(f"{NAME} ended; silent")
        return 0
    run(mode, now, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
