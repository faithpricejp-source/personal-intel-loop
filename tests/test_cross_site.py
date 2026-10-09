"""跨站请求防护：POST 只收本机/Tailscale 主机、application/json、同源 Origin。"""
from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from personal_intel_loop import web
from personal_intel_loop.store import connect_db, ensure_schema

EVENTS = json.dumps({"session_id": "s", "events": []}).encode("utf-8")


@pytest.fixture
def server(monkeypatch, tmp_path):
    db = tmp_path / "web.sqlite"
    conn = connect_db(db)
    ensure_schema(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(web, "DB_PATH", db)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web._Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def post(port, headers, body=EVENTS, path="/api/paper/events"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("POST", path, body=body, headers=headers)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def test_same_origin_json_allowed(server):
    h = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server}"}
    assert post(server, h)[0] == 200


def test_text_plain_simple_request_refused(server):
    assert post(server, {"Content-Type": "text/plain"})[0] == 403


def test_foreign_origin_refused(server):
    h = {"Content-Type": "application/json", "Origin": "http://evil.example"}
    assert post(server, h)[0] == 403


def test_rebound_host_refused(server):
    h = {"Content-Type": "application/json", "Host": f"evil.example:{server}"}
    assert post(server, h)[0] == 403


def test_tailnet_host_allowed(server):
    h = {"Content-Type": "application/json; charset=utf-8", "Host": "macbox.tailabc.ts.net:8443",
         "Origin": "https://macbox.tailabc.ts.net:8443"}
    assert post(server, h)[0] == 200


def test_non_object_json_gets_error_response_not_dropped(server):
    status, body = post(server, {"Content-Type": "application/json"}, body=b"[1]")
    assert status == 400 and b"JSON object" in body


def test_negative_content_length_refused(server):
    c = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    try:
        c.putrequest("POST", "/api/paper/events")
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", "-1")
        c.endheaders()
        r = c.getresponse()
        assert r.status == 400 and b"Content-Length" in r.read()
    finally:
        c.close()


@pytest.mark.parametrize("case_id", ["beacon_json_blob", "beacon_json_fetch_fallback"])
def test_frontend_beacon_sends_json(case_id):
    """前端收尾 beacon 若还用 text/plain, 会被上面的防护 403 且浏览器不报错 → 事件静默丢失。
    用 tests/frontend/fix_1005.js 的 vm 沙箱跑真实 app.js 的 trackBeacon。"""
    node = shutil.which("node")
    if node is None:
        pytest.fail("node 不可用：前端用例无法执行（tests/frontend/fix_1005.js）")
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([node, str(root / "tests" / "frontend" / "fix_1005.js"), case_id],
                          capture_output=True, text=True, cwd=str(root))
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0 and ("PASS " + case_id) in out, out
