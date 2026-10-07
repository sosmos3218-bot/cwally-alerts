#!/usr/bin/env python3
"""HBM blended-ASP baseline reminder. Standard library only. Not a trade signal."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "hbmasp.json"
STATE_PATH = ROOT / "state" / "hbmasp.json"
KST = timezone(timedelta(hours=9))
NOTICE_RE = re.compile(r"HBM|고대역폭|공급가|판매가|공급계약|단일판매", re.I)


def helper(name: str):
    try:
        return __import__(name)
    except ImportError:
        flat = ROOT.parent / "ntfy-source" / f"scripts__{name}.py"
        if not flat.exists():
            raise
        spec = importlib.util.spec_from_file_location(name, flat)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return copy.deepcopy(fallback)
    data = json.loads(path.read_text(encoding="utf-8"))
    out = copy.deepcopy(fallback)
    if isinstance(data, dict):
        out.update(data)
    return out


def atomic_save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def default_state() -> dict:
    return {
        "schema_version": 1,
        "last_baseline_week": None,
        "notice_versions": [],
        "ops_daily": {},
        "updated_at": None,
    }


def body(stamp: str, what: str, status: str, action: str, later: str, reduce: str, avoid: str, extra: list[str] | None = None) -> str:
    lines = [
        f"{stamp} | HBMASP",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {reduce}",
        f"하지 말 것: {avoid}",
    ]
    return "\n".join(lines + (extra or []))


def baseline_extra(cfg: dict) -> list[str]:
    b = cfg["baseline"]
    return [
        f"기준: {b['lock']}",
        b["offer_not_contract"],
        b["later_report"],
        b["trendforce"],
        b["kb"],
        b["sk"],
        b["goldman"],
        b["direction"],
    ]


def notice_key(code: str, item: dict) -> str:
    raw = "|".join((code, str(item.get("id") or ""), str(item.get("title") or ""), str(item.get("datetime") or "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def publish(ntfy, server: str, title: str, message: str, priority: int = 3) -> None:
    print(f"channel=cwally-port title={title}")
    print(message)
    ntfy.publish("cwally-port", title, message, priority, ["chart_with_upwards_trend"], server)


def install_confirm(now: datetime) -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ntfy = helper("ntfy_util")
    server = ntfy.resolve_server(cfg.get("ntfy_server"))
    stamp = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    message = body(
        stamp,
        "HBM 블렌디드 ASP 기준 알림 설치 확인입니다. 매매 신호가 아닙니다.",
        "비교 기준은 블렌디드 ASP. 제시가 3배는 확정가가 아님. 금액 민감도는 없음.",
        "확정 계약가와 회사 가이던스만 보고, 제시가를 영업이익에 넣지 마세요.",
        "같은 기관이 ASP 두 시나리오 영업이익을 공개하면 그때만 금액 비교.",
        "이 알림으로 줄이지 마세요. 최종가가 2배 안팎이면 3배 프레임을 폐기.",
        "연도가 다른 점추정을 차액으로 읽기. 4달러대 중후반을 확정 ASP로 읽기.",
        baseline_extra(cfg),
    )
    publish(ntfy, server, "[HBMASP] 설치 확인 | 매매 신호 아님", message, 3)
    print("install-confirm sent; state not written")
    return 0


def run(now: datetime | None = None) -> int:
    now = (now or datetime.now(KST)).astimezone(KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, default_state())
    work = copy.deepcopy(state)
    stamp, today = now.strftime("%Y-%m-%d %H:%M KST"), now.date().isoformat()
    if now.year != int(cfg["year_guard"]) or not (cfg["active_from"] <= today <= cfg["expire_date"]):
        print(f"{stamp} outside active window; silent")
        return 0
    ntfy = helper("ntfy_util")
    server = ntfy.resolve_server(cfg.get("ntfy_server"))
    changed = False
    week = now.strftime("%G-W%V")
    if now.weekday() == 0 and state.get("last_baseline_week") != week:
        message = body(
            stamp,
            "HBM 가격 기준 주간 점검입니다. 매수 신호가 아닙니다.",
            "비교 기준은 블렌디드 ASP. 제시가 3배는 확정가가 아님. 하이닉스 탄력성이 크고 삼성은 연결에서 희석.",
            "확정 계약가와 회사 가이던스만 확인하세요. 제시가를 영업이익에 넣지 마세요.",
            "같은 기관의 ASP 두 시나리오가 나오기 전에는 금액 민감도를 만들지 않습니다.",
            "이 알림으로 줄이지 마세요. 최종가가 2배 안팎이면 3배 프레임을 폐기.",
            "연도가 다른 점추정을 차액으로 읽기. 4달러대 중후반을 확정 ASP로 읽기.",
            baseline_extra(cfg),
        )
        try:
            publish(ntfy, server, "[HBMASP] 기준 유지 | 제시가를 확정가로 보지 말 것", message, 3)
            work["last_baseline_week"] = week
            changed = True
        except Exception as exc:
            print(f"publish failed baseline: {exc}", file=sys.stderr)
            return 1
    naver = helper("naver_api")
    seen = set(state.get("notice_versions") or [])
    errors = []
    for code, name in cfg["notices"]["codes"].items():
        try:
            items = naver.notices(code, size=cfg["notices"]["size"])
            if not isinstance(items, list):
                raise ValueError("공시 목록 형식 오류")
            for item in items:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                if not NOTICE_RE.search(item["title"]):
                    continue
                key = notice_key(code, item)
                if key in seen:
                    continue
                message = body(
                    stamp,
                    f"{name} HBM 가격·계약 제목 공시 원문 확인 요청입니다. 확정 판정이 아닙니다.",
                    f"제목: {item.get('title')}",
                    "원문에서 제시가인지 확정 계약가인지 직접 나누세요.",
                    "확정가 또는 회사 ASP 가이던스가 나오면 기준을 실적 민감도로 바꿉니다.",
                    "제목만으로 보유를 줄이지 마세요.",
                    "제목을 확정 계약, 3배 ASP, 영업이익 민감도로 읽기.",
                    [f"출처: {item.get('url') or '주소 없음'}"],
                )
                publish(ntfy, server, "[HBMASP] 새 공시 | 제시가와 확정가 구분", message, 4)
                work.setdefault("notice_versions", []).append(key)
                seen.add(key)
                work["notice_versions"] = work["notice_versions"][-500:]
                changed = True
        except Exception as exc:
            errors.append(f"{name} 공시 조회 실패: {exc}")
    if errors and state.get("ops_daily", {}).get(today) != "notice":
        ops = "[HBMASP] 공시 조회 실패 | 매매 신호 아님"
        message = body(
            stamp,
            "HBM 공시 목록 조회 실패 안내입니다. 매매 신호가 아닙니다.",
            " / ".join(errors),
            "다음 평일 09:00에 다시 봅니다. 오늘 기준은 유지하세요.",
            "원문이 없으면 제시가를 확정가로 올리지 않습니다.",
            "조회 실패로 보유를 줄이지 마세요.",
            "빈 결과를 계약 없음 또는 가격 확정으로 읽기.",
        )
        try:
            print(f"channel=cwally-ops title={ops}")
            ntfy.publish("cwally-ops", ops, message, 3, ["warning"], server)
            work.setdefault("ops_daily", {})[today] = "notice"
            changed = True
        except Exception as exc:
            print(f"ops publish failed: {exc}", file=sys.stderr)
    if changed:
        work["updated_at"] = stamp
        atomic_save(STATE_PATH, work)
        print("state updated")
    else:
        print(f"{stamp} no new baseline or notice")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-confirm", action="store_true")
    args = parser.parse_args()
    now = datetime.now(KST)
    if args.install_confirm:
        return install_confirm(now)
    return run(now)


if __name__ == "__main__":
    raise SystemExit(main())
