#!/usr/bin/env python3
"""Time-boxed FRED HTTP fetch for morning-bundle monitors.

A hanging or slowly dribbling FRED response must not eat the 20-minute
port-alerts job. Each series gets a short per-request timeout, a small
retry cap, and a hard total budget. Callers already catch failures and
fall back or skip; this module only bounds the wait.
"""
from __future__ import annotations

import concurrent.futures
import time
import urllib.request
from typing import Iterable

REQUEST_TIMEOUT = 12.0
MAX_ATTEMPTS = 2
SERIES_BUDGET = 60.0
DEFAULT_UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}

class FredFetchError(RuntimeError):
    """FRED (or a bounded fallback URL) could not be read in time or at all."""


def _open_and_read(url: str, timeout: float, headers: dict) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_text(
    url: str,
    *,
    timeout: float = REQUEST_TIMEOUT,
    attempts: int = MAX_ATTEMPTS,
    budget: float = SERIES_BUDGET,
    headers: dict | None = None,
    user_agents: Iterable[dict] | None = None,
) -> str:
    """GET url with a per-request timeout and a hard total budget.

    The wait is capped even when DNS or a slow body would ignore socket timeout.
    The worker thread may be abandoned on timeout; that is safe because each
    monitor is a short-lived process.
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
                last_err = FredFetchError(f"timed out after {req_timeout:.1f}s")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    raise FredFetchError(f"{type(last_err).__name__ if last_err else 'timeout'}: {last_err}")


def fetch_fred_csv(series: str, start: str, **kwargs) -> str:
    return fetch_text(
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}",
        **kwargs,
    )


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
            body = b"DATE,DGS10\n2026-11-20,4.92\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
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

    flaky_stop = threading.Event()

    class FlakyHandler(BaseHTTPRequestHandler):
        hits = 0

        def do_GET(self):
            type(self).hits += 1
            if type(self).hits == 1:
                flaky_stop.wait(30)
                return
            body = b"DATE,DGS10\n2026-11-20,4.92\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    def serve(handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        srv.daemon_threads = True
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        return srv

    def stop_srv(srv):
        # Do not wait for a hanging handler; daemon threads die with the process.
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass

    def url_of(srv):
        return f"http://127.0.0.1:{srv.server_address[1]}/dgs10"

    ok_srv = serve(OkHandler)
    t0 = time.monotonic()
    txt = fetch_text(url_of(ok_srv), timeout=2, attempts=2, budget=5)
    check("success csv", "DGS10" in txt and "4.92" in txt, f"{time.monotonic() - t0:.2f}s")
    stop_srv(ok_srv)

    hang_srv = serve(HangHandler)
    t0 = time.monotonic()
    raised = None
    try:
        fetch_text(url_of(hang_srv), timeout=1.0, attempts=2, budget=2.0)
    except FredFetchError as exc:
        raised = exc
    elapsed = time.monotonic() - t0
    check("hang raises", raised is not None, str(raised)[:80])
    check("hang respects budget", elapsed < 3.0, f"{elapsed:.2f}s")
    hang_stop.set()
    stop_srv(hang_srv)

    slow_srv = serve(SlowHandler)
    t0 = time.monotonic()
    raised = None
    try:
        fetch_text(url_of(slow_srv), timeout=2.0, attempts=2, budget=2.0)
    except FredFetchError as exc:
        raised = exc
    elapsed = time.monotonic() - t0
    check("slow body raises", raised is not None, str(raised)[:80])
    check("slow body respects budget", elapsed < 3.0, f"{elapsed:.2f}s")
    slow_stop.set()
    stop_srv(slow_srv)

    FlakyHandler.hits = 0
    flaky_srv = serve(FlakyHandler)
    t0 = time.monotonic()
    txt = fetch_text(url_of(flaky_srv), timeout=1.0, attempts=2, budget=3.0)
    elapsed = time.monotonic() - t0
    check("retry then success", "4.92" in txt, f"{elapsed:.2f}s hits={FlakyHandler.hits}")
    check("retry still under budget", elapsed < 4.0, f"{elapsed:.2f}s")
    flaky_stop.set()
    stop_srv(flaky_srv)

    # Connect to a socket that accepts and never replies (no HTTP).
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
        fetch_text(f"http://127.0.0.1:{port}/", timeout=1.0, attempts=2, budget=2.0)
    except FredFetchError as exc:
        raised = exc
    elapsed = time.monotonic() - t0
    check("silent accept raises", raised is not None, str(raised)[:80])
    check("silent accept respects budget", elapsed < 3.0, f"{elapsed:.2f}s")
    sock.close()

    # URL helper keeps the public series+start signature (no live FRED call here).
    check("fetch_fred_csv is callable", callable(fetch_fred_csv))

    if fails:
        print("SELFTEST FAIL: " + ", ".join(fails))
        return 1
    print("SELFTEST OK: hanging and slow FRED stay inside the budget, no live FRED call")
    return 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1].lower() == "selftest":
        raise SystemExit(_selftest())
    print("usage: fred_fetch.py selftest")
    raise SystemExit(2)
