import asyncio

import httpx

from ops.checks import run_check
from ops.config import ApiCheck, HttpCheck, PostgresCheck, TcpCheck


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_http_ok_and_bad_status():
    async with client(lambda r: httpx.Response(200, json={"status": "ok"})) as c:
        out = await run_check(HttpCheck(name="h", url="http://x/health"), c, 1)
        assert out.ok and out.status_code == 200 and out.extra["json"]["status"] == "ok"
    async with client(lambda r: httpx.Response(503)) as c:
        out = await run_check(HttpCheck(name="h", url="http://x/health"), c, 1)
        assert not out.ok and "503" in out.detail and out.critical


async def test_http_connection_error():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    async with client(boom) as c:
        out = await run_check(HttpCheck(name="h", url="http://x/health"), c, 1)
        assert not out.ok and "ConnectError" in out.detail


async def test_api_check_assertions():
    async with client(lambda r: httpx.Response(200, json={"orders": []})) as c:
        ok = await run_check(ApiCheck(name="a", url="http://x/o", expect_json_keys=["orders"]), c, 1)
        assert ok.ok and not ok.critical
        missing = await run_check(ApiCheck(name="a", url="http://x/o", expect_json_keys=["total"]), c, 1)
        assert not missing.ok and "total" in missing.detail


async def test_api_check_latency_budget():
    async def slow(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})

    async with client(slow) as c:
        out = await run_check(ApiCheck(name="a", url="http://x/o", max_latency_ms=10), c, 1)
        assert out.ok and out.slow and "budget" in out.detail


async def test_tcp_open_and_closed():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with client(lambda r: httpx.Response(200)) as c:
        out = await run_check(TcpCheck(name="t", host="127.0.0.1", port=port), c, 1)
        assert out.ok and out.latency_ms is not None
        server.close()
        await server.wait_closed()
        out = await run_check(TcpCheck(name="t", host="127.0.0.1", port=port), c, 1)
        assert not out.ok


async def test_postgres_missing_dsn(monkeypatch):
    monkeypatch.delenv("NOPE_DSN", raising=False)
    async with client(lambda r: httpx.Response(200)) as c:
        out = await run_check(PostgresCheck(name="pg", dsn_env="NOPE_DSN"), c, 1)
        assert not out.ok and "NOPE_DSN" in out.detail
