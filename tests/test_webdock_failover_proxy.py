import importlib.util
import json
import pathlib
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def load_proxy_module():
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "deploy" / "ecs" / "webdock-failover-proxy.py"
    spec = importlib.util.spec_from_file_location("webdock_failover_proxy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StubHandler(BaseHTTPRequestHandler):
    status = 200
    body = b'{"choices":[{"message":{"content":"ok"}}]}'
    content_type = "application/json"
    requests = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.__class__.requests.append((self.path, body))
        self.send_response(self.__class__.status)
        self.send_header("Content-Type", self.__class__.content_type)
        self.send_header("Content-Length", str(len(self.__class__.body)))
        self.end_headers()
        self.wfile.write(self.__class__.body)

    def log_message(self, _format, *args):
        return


class ServerContext:
    def __init__(self, handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server.server_address[1]

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class CloseImmediatelyServer:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        conn, _addr = self.sock.accept()
        conn.close()

    def __enter__(self):
        self.thread.start()
        return self.port

    def __exit__(self, exc_type, exc, tb):
        self.thread.join(timeout=2)
        self.sock.close()


class WebDockFailoverProxyTests(unittest.TestCase):
    def test_systemd_and_install_files_wire_failover_proxy(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        service = root / "deploy" / "ecs" / "webdock-failover-proxy.service"
        installer = root / "deploy" / "ecs" / "install-webdock-failover-proxy.sh"

        self.assertIn(
            "ExecStart=/usr/bin/python3 /opt/aliecs/webdock-failover-proxy.py",
            service.read_text(encoding="utf-8"),
        )
        installer_text = installer.read_text(encoding="utf-8")
        self.assertIn("WEBDOCK_FAILOVER_PRIMARY_PORT", installer_text)
        self.assertIn("WEBDOCK_FAILOVER_STANDBY_PORT", installer_text)
        self.assertIn("systemctl restart webdock-failover-proxy.service", installer_text)

    def test_defaults_listen_on_existing_bridge_port_and_split_primary_standby(self):
        proxy = load_proxy_module()

        config = proxy.parse_config({})

        self.assertEqual(config.bind_host, "127.0.0.1")
        self.assertEqual(config.bind_port, 11800)
        self.assertEqual(config.primary_port, 11810)
        self.assertEqual(config.standby_port, 11811)
        self.assertIn("已自动切换备用服务器", config.failover_prefix)

    def test_detects_webdock_cdp_503_as_retryable(self):
        proxy = load_proxy_module()
        body = json.dumps(
            {
                "detail": {
                    "code": "browser_not_started",
                    "message": "Chrome not running or CDP attach failed: Cannot connect",
                }
            }
        ).encode("utf-8")

        self.assertTrue(proxy.is_retryable_webdock_503(503, body))
        self.assertFalse(proxy.is_retryable_webdock_503(500, body))
        self.assertFalse(proxy.is_retryable_webdock_503(503, b'{"detail":{"message":"response timeout"}}'))

    def test_injects_failover_prefix_into_openai_json_content(self):
        proxy = load_proxy_module()
        body = json.dumps({"choices": [{"message": {"content": "normal reply"}}]}).encode("utf-8")

        injected = proxy.inject_failover_prefix(body, "PREFIX\n\n")
        payload = json.loads(injected.decode("utf-8"))

        self.assertEqual(payload["choices"][0]["message"]["content"], "PREFIX\n\nnormal reply")

    def test_proxy_retries_standby_when_primary_connection_fails(self):
        proxy = load_proxy_module()
        StubHandler.status = 200
        StubHandler.body = b'{"choices":[{"message":{"content":"standby reply"}}]}'
        StubHandler.requests = []

        with CloseImmediatelyServer() as primary_port:
            with ServerContext(StubHandler) as standby_port:
                config = proxy.ProxyConfig(
                    primary_port=primary_port,
                    standby_port=standby_port,
                    upstream_timeout_seconds=2.0,
                    primary_down_ttl_seconds=0.0,
                    failover_prefix="SWITCHED\n\n",
                )

                response = proxy.forward_with_failover(
                    "POST",
                    "/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    b'{"messages":[]}',
                    config,
                )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(StubHandler.requests), 1)
        self.assertIn(b"SWITCHED", response.body)
        self.assertIn(b"standby reply", response.body)

    def test_proxy_retries_standby_when_primary_reports_cdp_unavailable(self):
        proxy = load_proxy_module()

        class Primary503Handler(StubHandler):
            status = 503
            body = json.dumps(
                {"detail": {"message": "Chrome not running or CDP attach failed: Cannot connect"}}
            ).encode("utf-8")
            requests = []

        class StandbyHandler(StubHandler):
            status = 200
            body = b'{"choices":[{"message":{"content":"standby reply"}}]}'
            requests = []

        with ServerContext(Primary503Handler) as primary_port:
            with ServerContext(StandbyHandler) as standby_port:
                config = proxy.ProxyConfig(
                    primary_port=primary_port,
                    standby_port=standby_port,
                    upstream_timeout_seconds=2.0,
                    primary_down_ttl_seconds=0.0,
                    failover_prefix="SWITCHED\n\n",
                )

                response = proxy.forward_with_failover(
                    "POST",
                    "/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    b'{"messages":[]}',
                    config,
                )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(Primary503Handler.requests), 1)
        self.assertEqual(len(StandbyHandler.requests), 1)
        self.assertIn(b"SWITCHED", response.body)

    def test_proxy_does_not_retry_ambiguous_primary_timeout(self):
        proxy = load_proxy_module()

        class SlowPrimaryHandler(StubHandler):
            status = 200
            body = b'{"choices":[{"message":{"content":"late primary"}}]}'
            requests = []

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.__class__.requests.append((self.path, body))
                time.sleep(1.0)
                self.send_response(self.__class__.status)
                self.send_header("Content-Type", self.__class__.content_type)
                self.send_header("Content-Length", str(len(self.__class__.body)))
                self.end_headers()
                try:
                    self.wfile.write(self.__class__.body)
                except OSError:
                    pass

        class StandbyHandler(StubHandler):
            status = 200
            body = b'{"choices":[{"message":{"content":"standby reply"}}]}'
            requests = []

        with ServerContext(SlowPrimaryHandler) as primary_port:
            with ServerContext(StandbyHandler) as standby_port:
                config = proxy.ProxyConfig(
                    primary_port=primary_port,
                    standby_port=standby_port,
                    upstream_timeout_seconds=0.2,
                    primary_down_ttl_seconds=0.0,
                )

                with self.assertRaises(TimeoutError):
                    proxy.forward_with_failover(
                        "POST",
                        "/v1/chat/completions",
                        {"Content-Type": "application/json"},
                        b'{"messages":[]}',
                        config,
                    )

        self.assertEqual(len(SlowPrimaryHandler.requests), 1)
        self.assertEqual(len(StandbyHandler.requests), 0)


if __name__ == "__main__":
    unittest.main()
