#!/usr/bin/env python3
"""Time-boxed Daum finance HTTP fetch for morning and k203780/KR-HC monitors.

A hanging Daum days request used to retry three times at a 20s socket timeout
(about 70s per call, more with pagination). That ate the 20-minute port-alerts
job. Each call now has a short per-request timeout, at most two attempts, and
a hard per-call budget. All Daum calls in one process also share a total
budget; later calls fail fast when it is spent. Non-Daum fallbacks (Naver
minute bars, Yahoo, 확인 불가) are left to the caller.
"""
from __future__ import annotations

import concurrent.futures
import time
import urllib.request
from typing import Iterable

REQUEST_TIMEOUT = 10.0
MAX_ATTEMPTS = 2
CALL_BUDGET = 30.0
PROCESS_BUDGET = 180.0
DEFAULT_UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}

_spent = 0.0


class DaumFetchError(RuntimeError):
    """Daum (or a bounded quote URL) could not be read in time or at all."""


def reset_budget() -> None:
    """Test helper: clear process-wide spend."""
    global _spent
    _spent = 0.0


def process_spent() -> float:
    return _spent


def remaining_process_budget() -> float:
    return PROCESS_BUDGET - _spent


def _open_and_read(url: str, timeout: float, headers: dict) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def bounded_get(
    url: str,
    *,
    timeout: float = REQUEST_TIMEOUT,
    attempts: int = MAX_ATTEMPTS,
    budget: float = CALL_BUDGET,
    headers: dict | None = None,
    user_agents: Iterable[dict] | None = None,
) -> bytes:
    """GET url with a per-request timeout and a hard call budget.

    The wait is capped even when DNS or a slow body would ignore socket timeout.
    Does not consume the process Daum budget (use fetch_bytes for that).
    """
    if user_agents is not None:
        agents = [dict(h) for h in user_agents]
    elif headers is not None:
        agents = [dict(headers)]
    else:
        agents = [dict(DEFAULT_UA)]
    if not agents:
        agents = [dict(DEFAULT_UA)]
    attempts = max(1, int(attempts))
    deadline = time.monotonic() + float(budget)
    last_err: Exception | None = None
    for i in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            break
        req_timeout = min(float(timeout), remaining)
        hdrs = agents[i % len(agents)]
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(_open_and_read, url, req_timeout, hdrs)
            wait = min(req_timeout + 0.25, remaining + 0.05)
            try:
                return fut.result(timeout=wait)
            except concurrent.futures.TimeoutError:
                last_err = DaumFetchError(f"timed out after {req_timeout:.1f}s")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    raise DaumFetchError(f"{type(last_err).__name__ if last_err else 'timeout'}: {last_err}")


def fetch_bytes(
    url: str,
    *,
    timeout: float = REQUEST_TIMEOUT,
    attempts: int = MAX_ATTEMPTS,
    budget: float = CALL_BUDGET,
    headers: dict | None = None,
    user_agents: Iterable[dict] | None = None,
) -> bytes:
    """Daum GET. Fails fast if the process budget is spent, and counts time against it."""
    global _spent
    left = PROCESS_BUDGET - _spent
    if left <= 0.05:
        raise DaumFetchError("Daum process budget exhausted")
    use = min(float(budget), left)
    t0 = time.monotonic()
    try:
        return bounded_get(
            url,
            timeout=timeout,
            attempts=attempts,
            budget=use,
            headers=headers,
            user_agents=user_agents,
        )
    finally:
        _spent += time.monotonic() - t0


def fetch_text(url: str, **kwargs) -> str:
    return fetch_bytes(url, **kwargs).decode("utf-8", errors="replace")


def _selftest() -> int:
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("ok   " if ok else "FAIL ") + name + (f" {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    class OkHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"data":[{"symbolCode":"A005930","date":"2026-11-20","tradePrice":100000}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    hang_stop = threading.Event()

    class HangHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            hang_stop.wait(30)

        def log_message(self, *args):
            pass

    slow_stop = threading.Event()

    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                for _ in range(40):
                    if slow_stop.is_set():
                        return
                    self.wfile.write(b".")
                    self.wfile.flush()
                    slow_stop.wait(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    def serve(handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        srv.daemon_threads = True
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        return srv

    def stop_srv(srv):
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass

    def url_of(srv):
        return f"http://127.0.0.1:{srv.server_address[1]}/quote"

    reset_budget()
    ok_srv = serve(OkHandler)
    t0 = time.monotonic()
    raw = bounded_get(url_of(ok_srv), timeout=2, attempts=2, budget=5)
    check("success json", b"tradePrice" in raw, f"{time.monotonic() - t0:.2f}s")
    stop_srv(ok_srv)

    hang_stop.clear()
    hang_srv = serve(HangHandler)
    t0 = time.monotonic()
    raised = None
    try:
        bounded_get(url_of(hang_srv), timeout=1.0, attempts=2, budget=2.0)
    except DaumFetchError as exc:
        raised = exc
    elapsed = time.monotonic() - t0
    check("hang raises", raised is not None, str(raised)[:80])
    check("hang respects budget", elapsed < 3.0, f"{elapsed:.2f}s")
    hang_stop.set()
    stop_srv(hang_srv)

    slow_stop.clear()
    slow_srv = serve(SlowHandler)
    t0 = time.monotonic()
    raised = None
    try:
        bounded_get(url_of(slow_srv), timeout=2.0, attempts=2, budget=2.0)
    except DaumFetchError as exc:
        raised = exc
    elapsed = time.monotonic() - t0
    check("slow body raises", raised is not None, str(raised)[:80])
    check("slow body respects budget", elapsed < 3.0, f"{elapsed:.2f}s")
    slow_stop.set()
    stop_srv(slow_srv)

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def _accept():
        try:
            conn, _ = sock.accept()
            time.sleep(30)
            conn.close()
        except OSError:
            pass

    threading.Thread(target=_accept, daemon=True).start()
    t0 = time.monotonic()
    raised = None
    try:
        bounded_get(f"http://127.0.0.1:{port}/", timeout=1.0, attempts=2, budget=2.0)
    except DaumFetchError as exc:
        raised = exc
    elapsed = time.monotonic() - t0
    check("silent accept raises", raised is not None, str(raised)[:80])
    check("silent accept respects budget", elapsed < 3.0, f"{elapsed:.2f}s")
    sock.close()

    # Process budget: first hanging call spends it, later calls fail fast.
    reset_budget()
    global PROCESS_BUDGET
    saved = PROCESS_BUDGET
    PROCESS_BUDGET = 1.2
    hang_stop.clear()
    hang_srv = serve(HangHandler)
    t0 = time.monotonic()
    raised = None
    try:
        fetch_bytes(url_of(hang_srv), timeout=1.0, attempts=2, budget=1.2)
    except DaumFetchError as exc:
        raised = exc
    first = time.monotonic() - t0
    check("process first call times out", raised is not None, f"{first:.2f}s")
    t0 = time.monotonic()
    raised = None
    try:
        fetch_bytes(url_of(hang_srv), timeout=1.0, attempts=2, budget=1.2)
    except DaumFetchError as exc:
        raised = exc
    second = time.monotonic() - t0
    check("process budget fail fast", raised is not None and "process budget" in str(raised), str(raised)[:80])
    check("fail fast is quick", second < 0.3, f"{second:.3f}s")
    hang_stop.set()
    stop_srv(hang_srv)
    PROCESS_BUDGET = saved
    reset_budget()

    check("callable fetch_text", callable(fetch_text))

    if fails:
        print("SELFTEST FAIL: " + ", ".join(fails))
        return 1
    print("SELFTEST OK: hanging and slow Daum stay inside the budget, later calls fail fast, no live Daum call")
    return 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1].lower() == "selftest":
        raise SystemExit(_selftest())
    print("usage: daum_fetch.py selftest")
    raise SystemExit(2)
