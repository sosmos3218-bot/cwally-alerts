#!/usr/bin/env python3
"""[BRIEF] 일회성 브리프 발송기. 예약 비서가 쓴 요약(예: 실적 정리)을 ntfy 로 직접 보내지 않고 이 저장소를 거쳐 보낸다.

outbox/<YYYYMMDD>-<slug>.json = {"topic": "cwally-port"|"cwally-ops", "title": "...", "body": "...",
                                 "priority": 1-5 (기본 3), "tags": [..] (선택)}
실행할 때마다 state/brief.json 에 없는 파일(파일명 + 내용 sha256 기준)을 ntfy_util.publish 로 보내고 기록한다.
- title 필수. topic 은 cwally-port / cwally-ops 만 (다른 값은 거부, cwally-ops 에 안내).
- body 가 ntfy 한도를 넘으면 자르지 않고 (1/n) 조각으로 나눠 보낸다 (줄 경계 우선, 이어 붙이면 원문과 같다).
- 그 밖의 내용은 지우거나 거부하지 않는다. 제목 맨 앞 [PREFIX] 가 없으면 [BRIEF] 를 붙인다.
  한국어 가운뎃점/엠대시는 고치지 않고 경고만 찍는다 (작성 규칙은 AGENTS.md).
- 해석 실패/발송 실패는 cwally-ops 안내 (파일+내용당 1번), 발송 실패는 다음 실행에서 남은 조각부터 다시.
워크플로에서는 job=brief dispatch 일 때만 돈다. oneoff_mode=auto 일 때만 실제 발송, 나머지는 dry.
modes: auto | dry(발송 없음, state 안 씀) | selftest(가짜 파일, 네트워크 없음)
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ntfy_util  # noqa: E402
from ntfy_util import KST, route_topic  # noqa: E402

NAME = "brief"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FILE_RE = re.compile(r"^\d{8}-[A-Za-z0-9][A-Za-z0-9._-]*\.json$")
PREFIX_RE = re.compile(r"^\s*\[[^\]]+\]")
BAD_CHARS = ("\u00b7", "\u2014")


def load_state(path: Path) -> dict:
    base = {"updated_at": None, "sent": {}, "rejected": {}, "fail_notice": {}}
    if path.exists():
        try:
            base.update(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def file_key(name: str, raw: bytes) -> str:
    return f"{name}#{hashlib.sha256(raw).hexdigest()[:16]}"


def parse_brief(raw: bytes, cfg: dict) -> tuple[dict | None, str | None, list[str]]:
    """(brief, 거부 사유, 경고). 거부는 JSON 아님/객체 아님/title 없음/topic 허용 밖/body 문자열 아님뿐."""
    warn: list[str] = []
    try:
        d = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"JSON 해석 불가 ({str(exc)[:120]})", warn
    if not isinstance(d, dict):
        return None, "JSON 최상위가 객체가 아님", warn
    title = d.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, "title 없음", warn
    topic = d.get("topic")
    if topic not in cfg["allowed_topics"]:
        return None, f"topic {topic!r} 허용 안 됨 (cwally-port, cwally-ops 만)", warn
    body = d.get("body", "")
    if body is None:
        body = ""
    if not isinstance(body, str):
        return None, "body 가 문자열이 아님", warn
    pr = d.get("priority", cfg["default_priority"])
    if isinstance(pr, bool) or not isinstance(pr, (int, float)) or int(pr) != pr or not 1 <= int(pr) <= 5:
        warn.append(f"priority {pr!r} -> {cfg['default_priority']} (1~5 정수만)")
        pr = cfg["default_priority"]
    tags = d.get("tags", None)
    if tags is None:
        tags = list(cfg["default_tags"])
    elif isinstance(tags, str):
        tags = [tags]
    elif not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        warn.append(f"tags {tags!r} 무시 (문자열 목록만)")
        tags = list(cfg["default_tags"])
    title = title.strip()
    if not PREFIX_RE.match(title):
        warn.append(f"제목 prefix 없음 -> {cfg['default_title_prefix']} 붙임")
        title = f"{cfg['default_title_prefix']} {title}"
    for ch, nm in zip(BAD_CHARS, ("가운뎃점", "엠대시")):
        if ch in title + body:
            warn.append(f"{nm} 포함 (그대로 보냄, AGENTS.md 한국어 규칙 위반)")
    extra = sorted(set(d) - {"topic", "title", "body", "priority", "tags"})
    if extra:
        warn.append(f"모르는 칸 무시: {extra}")
    return {"topic": topic, "title": title, "body": body, "priority": int(pr), "tags": tags}, None, warn


def _cut_bytes(s: str, limit: int) -> tuple[str, str]:
    """UTF-8 바이트 limit 이하로 앞부분을 자른다 (글자 중간을 자르지 않음)."""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s, ""
    cut = limit
    while cut > 0 and (b[cut] & 0xC0) == 0x80:
        cut -= 1
    head = b[:cut].decode("utf-8")
    return head, s[len(head):]


def split_body(body: str, limit: int, tail_reserve: int = 0) -> list[str]:
    """limit 바이트 이하 조각들. 마지막 조각은 tail_reserve 만큼 더 비운다. ''.join(parts) == body."""
    if len(body.encode("utf-8")) + tail_reserve <= limit:
        return [body]
    parts: list[str] = []
    cur = ""
    for line in body.splitlines(keepends=True):
        while line:
            room = limit - len(cur.encode("utf-8"))
            if len(line.encode("utf-8")) <= room:
                cur += line
                line = ""
            elif cur:
                parts.append(cur)
                cur = ""
            else:
                head, line = _cut_bytes(line, limit)
                parts.append(head)
    if cur:
        parts.append(cur)
    if len(parts[-1].encode("utf-8")) + tail_reserve > limit:  # 끝줄 붙을 자리
        head, rest = _cut_bytes(parts[-1], limit - tail_reserve)
        k = head.rfind("\n")
        if 0 < k < len(head) - 1:
            head, rest = head[:k + 1], head[k + 1:] + rest
        parts[-1:] = [head, rest] if head else [rest]
    return parts


def make_parts(b: dict, cfg: dict, ends_bytes: int) -> list[tuple[str, str]]:
    chunks = split_body(b["body"], int(cfg["message_max_bytes"]), ends_bytes + 1)
    n = len(chunks)
    if n == 1:
        return [(b["title"], chunks[0])]
    return [(f"{b['title']} ({i}/{n})", c) for i, c in enumerate(chunks, 1)]


def ops_body(stamp: str, what: str, status: str) -> str:
    return "\n".join([f"{stamp} | BRIEF | 브리프 발송", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      "지금 할 일: outbox 파일을 고쳐 커밋한 뒤 job=brief 를 다시 dispatch.",
                      "나중에: 고친 파일은 내용이 바뀌어 새 브리프로 보냄.", "익절/줄일 때: 해당 없음.",
                      "하지 말 것: ntfy 로 직접 보내기."])


def run(mode: str, cfg: dict, outbox: Path, state_path: Path, publish=None, now: datetime | None = None) -> int:
    publish = publish or ntfy_util.publish
    now = now or datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    dry = mode == "dry"
    state = load_state(state_path)
    files = sorted(p for p in outbox.glob("*.json") if p.is_file()) if outbox.is_dir() else []
    print(f"now={stamp} mode={mode} outbox={outbox} files={len(files)} sent_records={len(state['sent'])}")
    ends = ntfy_util.ends_line(NAME) or ""
    ends_bytes = len(ends.encode("utf-8"))
    failures = 0
    sent_n = 0

    def ops(key: str, title: str, msg: str) -> None:
        if state["fail_notice"].get(key):
            print(f"ops notice already sent for {key}")
            return
        print(f"OPS TITLE: {title}\n{msg}\n")
        if not dry:
            try:
                publish("cwally-ops", title, msg, 3, ["warning"])
                state["fail_notice"][key] = now.isoformat()
            except Exception as exc:  # noqa: BLE001
                print(f"ops publish failed: {exc}", file=sys.stderr)

    for p in files:
        raw = p.read_bytes()
        key = file_key(p.name, raw)
        rec = state["sent"].get(key)
        if rec and rec.get("done"):
            print(f"skip (already sent): {key}")
            continue
        if key in state["rejected"]:
            print(f"skip (rejected before): {key} {state['rejected'][key].get('why')}")
            continue
        if not FILE_RE.match(p.name):
            print(f"warn: {p.name} 파일명이 <YYYYMMDD>-<slug>.json 형식이 아님 (그래도 처리)")
        b, why, warns = parse_brief(raw, cfg)
        for w in warns:
            print(f"warn: {p.name}: {w}")
        if b is None:
            print(f"REJECT {p.name}: {why}")
            ops(f"reject:{key}", f"[BRIEF] 브리프 파일 해석 불가 | {p.name}",
                ops_body(stamp, "outbox 브리프 파일을 보내지 못함.", f"{p.name}: {why}. 이 파일은 보내지 않음."))
            if not dry:
                state["rejected"][key] = {"why": why, "at": now.isoformat()}
            continue
        parts = make_parts(b, cfg, ends_bytes)
        done = int((rec or {}).get("parts_done", 0))
        topic = route_topic(b["topic"], b["title"])
        if topic != b["topic"]:
            print(f"note: 제목에 실패 안내 단어가 있어 ntfy_util 이 {topic} 로 보냄")
        for i, (t, m) in enumerate(parts, 1):
            last = i == len(parts)
            nbytes = len(m.encode("utf-8")) + (ends_bytes + 1 if last else 0)
            if i <= done:
                print(f"skip part {i}/{len(parts)} (sent before)")
                continue
            print(f"TITLE: {t}\nTOPIC: {topic} PRIORITY: {b['priority']} TAGS: {b['tags']} BYTES: {nbytes}\n{m}\n")
            if dry:
                continue
            try:
                publish(b["topic"], t, m, b["priority"], b["tags"], append_end=last)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"publish failed {p.name} part {i}: {exc}", file=sys.stderr)
                ops(f"send:{key}", f"[BRIEF] 브리프 발송 실패 | {p.name}",
                    ops_body(stamp, "outbox 브리프 발송 중 ntfy 오류.",
                             f"{p.name} {i}/{len(parts)} 조각에서 실패 ({str(exc)[:150]}). 앞 조각 {i - 1}개는 이미 보냄. "
                             "다음 job=brief 실행 때 남은 조각부터 다시 보냄."))
                break
            skip = ntfy_util.LAST_PUBLISH.get("sent") is False
            done = i
            state["sent"][key] = {"file": p.name, "title": b["title"], "topic": topic, "parts": len(parts),
                                  "parts_done": done, "done": done == len(parts), "at": now.isoformat(),
                                  **({"skipped": ntfy_util.LAST_PUBLISH.get("reason")} if skip else {})}
            if skip:
                print(f"publish skipped by ends_kst: {ntfy_util.LAST_PUBLISH.get('reason')}")
                state["sent"][key]["done"] = True
                break
        else:
            if not dry:
                sent_n += 1
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    keep = int(cfg.get("state_keep", 500))
    for k in ("sent", "rejected"):
        state[k] = dict(sorted(state[k].items(), key=lambda kv: kv[1].get("at") or "")[-keep:])
    state["updated_at"] = now.isoformat()
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"sent briefs: {sent_n}, failures: {failures}")
    return 1 if failures else 0


def selftest(cfg: dict) -> int:
    """가짜 outbox 와 가짜 publish 로 검사 (네트워크, ntfy, 실제 state 없음)."""
    sent: list[dict] = []

    def fake_publish(topic, title, message, priority=3, tags=None, server=None, **kw):
        if kw.get("append_end", True):
            message = ntfy_util.append_ends(message, NAME)
        sent.append({"topic": ntfy_util.route_topic(topic, title), "title": title, "message": message,
                     "priority": priority, "tags": tags})
        ntfy_util.LAST_PUBLISH.clear()
        ntfy_util.LAST_PUBLISH.update({"sent": True})

    limit = int(cfg["message_max_bytes"])
    ends = ntfy_util.ends_line(NAME) or ""
    # 1) 나누기: 원문 그대로 이어지고, 조각마다 한도 이하, 글자 안 깨짐
    long_body = "\n".join(f"{i:03d} 실적 요약 줄입니다. 매출 +12%, 영업이익률 18.5%." for i in range(300))
    parts = split_body(long_body, limit, len(ends.encode()) + 1)
    assert len(parts) > 1 and "".join(parts) == long_body
    assert all(len(x.encode()) <= limit for x in parts)
    assert len(parts[-1].encode()) + len(ends.encode()) + 1 <= limit
    one_line = "가" * 5000  # 줄바꿈 없는 긴 줄 (15000 바이트)
    p2 = split_body(one_line, limit, 50)
    assert "".join(p2) == one_line and all(len(x.encode()) <= limit for x in p2) and len(p2) == 4, [len(x.encode()) for x in p2]
    assert split_body("짧은 본문", limit, 50) == ["짧은 본문"]
    # 2) 검증
    ok, why, _ = parse_brief(json.dumps({"topic": "cwally-port", "title": "[EARN] t", "body": "b"}).encode(), cfg)
    assert ok and ok["priority"] == 3 and ok["tags"] == ["memo"] and why is None
    for bad, reason in (({"topic": "cwally-flow", "title": "x", "body": "b"}, "topic"),
                        ({"topic": "clarity-52505089", "title": "x"}, "topic"),
                        ({"title": "x", "body": "b"}, "topic"),
                        ({"topic": "cwally-port", "body": "b"}, "title"),
                        ({"topic": "cwally-port", "title": "  ", "body": "b"}, "title")):
        b, why, _ = parse_brief(json.dumps(bad).encode(), cfg)
        assert b is None and reason in why, (bad, why)
    b, why, _ = parse_brief(b"{not json", cfg)
    assert b is None and "JSON" in why
    b, _, w = parse_brief(json.dumps({"topic": "cwally-ops", "title": "no prefix", "body": "가\u00b7나", "priority": 9,
                                      "tags": "chart"}).encode(), cfg)
    assert b["title"] == "[BRIEF] no prefix" and b["priority"] == 3 and b["tags"] == ["chart"] and "가\u00b7나" == b["body"]
    assert any("가운뎃점" in x for x in w)
    # 3) 실행: 발송, 해시 중복 방지, 내용 바뀌면 다시, 거부는 ops 1번 (run 출력은 숨김)
    with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
        ob, st = Path(td) / "outbox", Path(td) / "brief.json"
        ob.mkdir()
        (ob / "20261029-earn-recap.json").write_text(json.dumps(
            {"topic": "cwally-port", "title": "[EARN] 10/29 실적 정리", "body": long_body, "priority": 4,
             "tags": ["memo", "chart_with_upwards_trend"]}, ensure_ascii=False), encoding="utf-8")
        (ob / "20261029-bad-topic.json").write_text(json.dumps({"topic": "cwally-flow", "title": "[BRIEF] x", "body": "y"}),
                                                    encoding="utf-8")
        (ob / "20261029-short.json").write_text(json.dumps({"topic": "cwally-port", "title": "[BRIEF] 짧은 메모",
                                                            "body": "**굵게** 한 줄"}, ensure_ascii=False), encoding="utf-8")
        (ob / "notes.txt").write_text("ignored", encoding="utf-8")
        now = datetime(2026, 10, 29, 21, 0, tzinfo=KST)
        assert run("dry", cfg, ob, st, fake_publish, now) == 0 and not sent and not st.exists()
        assert run("auto", cfg, ob, st, fake_publish, now) == 0
        port = [s for s in sent if s["topic"] == "cwally-port"]
        opsn = [s for s in sent if s["topic"] == "cwally-ops"]
        n = len(parts)
        assert len(port) == n + 1, [s["title"] for s in sent]
        earn = [s for s in port if s["title"].startswith("[EARN]")]
        assert [s["title"] for s in earn] == [f"[EARN] 10/29 실적 정리 ({i}/{n})" for i in range(1, n + 1)]
        assert all(len(s["message"].encode()) <= 4096 for s in sent)
        assert sum(ntfy_util.ENDS_PREFIX in s["message"] for s in earn) == 1 and ntfy_util.ENDS_PREFIX in earn[-1]["message"]
        assert "".join(s["message"] for s in earn).replace("\n" + ends, "") == long_body
        assert all(s["priority"] == 4 for s in earn)
        assert len(opsn) == 1 and "해석 불가" in opsn[0]["title"] and "cwally-flow" in opsn[0]["message"]
        before = len(sent)
        assert run("auto", cfg, ob, st, fake_publish, now) == 0 and len(sent) == before  # 같은 파일+내용: 다시 안 보냄
        (ob / "20261029-short.json").write_text(json.dumps({"topic": "cwally-port", "title": "[BRIEF] 짧은 메모",
                                                            "body": "고친 내용"}, ensure_ascii=False), encoding="utf-8")
        assert run("auto", cfg, ob, st, fake_publish, now) == 0 and len(sent) == before + 1  # 내용 바뀜: 새로 보냄
        s = json.loads(st.read_text(encoding="utf-8"))
        assert len(s["sent"]) == 3 and len(s["rejected"]) == 1
        # 4) 중간 실패: 남은 조각부터 다시
        st.unlink()
        calls = {"n": 0}

        def flaky(topic, title, message, priority=3, tags=None, server=None, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("ntfy 503")
            fake_publish(topic, title, message, priority, tags, server, **kw)
        sent.clear()
        (ob / "20261029-bad-topic.json").unlink()
        (ob / "20261029-short.json").unlink()
        assert run("auto", cfg, ob, st, flaky, now) == 1
        assert [x["title"] for x in sent if x["topic"] == "cwally-port"] == [f"[EARN] 10/29 실적 정리 (1/{n})"]
        assert any("발송 실패" in x["title"] and x["topic"] == "cwally-ops" for x in sent)
        sent.clear()
        assert run("auto", cfg, ob, st, fake_publish, now) == 0
        assert [x["title"] for x in sent] == [f"[EARN] 10/29 실적 정리 ({i}/{n})" for i in range(2, n + 1)]
    print(f"SELFTEST OK: split {n} parts (max {max(len(x.encode()) for x in parts)} bytes), dedupe, topic reject, resume. "
          "no network, no publish, no state")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    return run(mode, cfg, ROOT / cfg.get("outbox_dir", "outbox"), STATE_PATH)


if __name__ == "__main__":
    sys.exit(main())
