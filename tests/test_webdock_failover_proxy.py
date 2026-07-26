import importlib.util
import pathlib
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock


def load_proxy_module():
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "deploy" / "ecs" / "webdock-failover-proxy.py"
    spec = importlib.util.spec_from_file_location("webdock_failover_proxy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FailoverProxyConfigTests(unittest.TestCase):
    def test_device_names_default_empty(self):
        proxy = load_proxy_module()
        config = proxy.parse_config({})

        self.assertEqual(config.primary_name, "")
        self.assertEqual(config.standby_name, "")

    def test_device_names_from_env(self):
        proxy = load_proxy_module()
        config = proxy.parse_config(
            {
                "WEBDOCK_FAILOVER_PRIMARY_NAME": " webdock1 ",
                "WEBDOCK_FAILOVER_STANDBY_NAME": "webdock2",
            }
        )

        self.assertEqual(config.primary_name, "webdock1")
        self.assertEqual(config.standby_name, "webdock2")


class AnnotateRouteTests(unittest.TestCase):
    def test_primary_route_headers(self):
        proxy = load_proxy_module()
        config = proxy.parse_config({"WEBDOCK_FAILOVER_PRIMARY_NAME": "webdock1"})
        response = proxy.UpstreamResponse(200, "OK", {"Content-Type": "application/json"}, b"{}")

        annotated = proxy.annotate_route(response, config, "primary")

        self.assertEqual(annotated.headers["X-Webdock-Route"], "primary")
        self.assertEqual(annotated.headers["X-Webdock-Device"], "webdock1")
        self.assertEqual(annotated.headers["Content-Type"], "application/json")
        self.assertEqual(annotated.body, b"{}")

    def test_standby_route_headers(self):
        proxy = load_proxy_module()
        config = proxy.parse_config({"WEBDOCK_FAILOVER_STANDBY_NAME": "webdock2"})
        response = proxy.UpstreamResponse(200, "OK", {}, b"{}")

        annotated = proxy.annotate_route(response, config, "standby")

        self.assertEqual(annotated.headers["X-Webdock-Route"], "standby")
        self.assertEqual(annotated.headers["X-Webdock-Device"], "webdock2")

    def test_no_device_header_when_name_unset(self):
        proxy = load_proxy_module()
        config = proxy.parse_config({})
        response = proxy.UpstreamResponse(200, "OK", {}, b"{}")

        annotated = proxy.annotate_route(response, config, "primary")

        self.assertEqual(annotated.headers["X-Webdock-Route"], "primary")
        self.assertNotIn("X-Webdock-Device", annotated.headers)

    def test_original_response_headers_not_mutated(self):
        proxy = load_proxy_module()
        config = proxy.parse_config({"WEBDOCK_FAILOVER_PRIMARY_NAME": "webdock1"})
        original_headers = {"Content-Type": "application/json"}
        response = proxy.UpstreamResponse(200, "OK", original_headers, b"{}")

        proxy.annotate_route(response, config, "primary")

        self.assertNotIn("X-Webdock-Route", original_headers)


class RequestLedgerFailoverTests(unittest.TestCase):
    def setUp(self):
        self.proxy = load_proxy_module()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.ledger_path = str(pathlib.Path(self.tempdir.name) / "requests.sqlite3")
        self.config = self.proxy.parse_config(
            {
                "WEBDOCK_FAILOVER_LEDGER_PATH": self.ledger_path,
                "WEBDOCK_FAILOVER_PRIMARY_NAME": "webdock2",
                "WEBDOCK_FAILOVER_STANDBY_NAME": "webdock1",
                "WEBDOCK_FAILOVER_UPSTREAM_TIMEOUT_SECONDS": "320",
            }
        )
        self.ledger = self.proxy.RequestLedger(self.ledger_path, 604800)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(pathlib.Path(self.ledger_path).stat().st_mode), 0o640)
        self.headers = {"X-Request-ID": "req-001", "Content-Type": "application/json"}
        self.body = b'{"messages":[{"role":"user","content":"hello"}]}'
        self.proxy._primary_down_until = 0.0

    def forward(self):
        return self.proxy.forward_with_failover(
            "POST",
            "/v1/chat/completions",
            self.headers,
            self.body,
            self.config,
            self.ledger,
        )

    def test_connect_failure_before_submit_fails_over(self):
        calls = []

        def fake_forward(method, path, headers, body, host, port, timeout, on_connected=None):
            calls.append(port)
            if port == self.config.primary_port:
                raise self.proxy.PreSubmitConnectionError("refused")
            if callable(on_connected):
                on_connected()
            return self.proxy.UpstreamResponse(200, "OK", {}, b"{}")

        with mock.patch.object(self.proxy, "forward_once", side_effect=fake_forward):
            response = self.forward()

        self.assertEqual(calls, [self.config.primary_port, self.config.standby_port])
        self.assertEqual(response.headers["X-Webdock-Route"], "standby")

    def test_explicit_cdp_503_fails_over(self):
        calls = []

        def fake_forward(method, path, headers, body, host, port, timeout, on_connected=None):
            calls.append(port)
            if callable(on_connected):
                on_connected()
            if port == self.config.primary_port:
                return self.proxy.UpstreamResponse(
                    503,
                    "Service Unavailable",
                    {"Content-Type": "application/json"},
                    b'{"detail":{"message":"CDP attach failed"}}',
                )
            return self.proxy.UpstreamResponse(200, "OK", {}, b"{}")

        with mock.patch.object(self.proxy, "forward_once", side_effect=fake_forward):
            response = self.forward()

        self.assertEqual(calls, [self.config.primary_port, self.config.standby_port])
        self.assertEqual(response.headers["X-Webdock-Route"], "standby")

    def test_disconnect_after_connect_is_unknown_and_never_fails_over(self):
        calls = []

        def fake_forward(method, path, headers, body, host, port, timeout, on_connected=None):
            calls.append(port)
            if callable(on_connected):
                on_connected()
            raise self.proxy.DeliveryUnknownError("reset after submit")

        with mock.patch.object(self.proxy, "forward_once", side_effect=fake_forward):
            with self.assertRaises(self.proxy.DeliveryUnknownError):
                self.forward()

        self.assertEqual(calls, [self.config.primary_port])
        connection = sqlite3.connect(self.ledger_path)
        try:
            state = connection.execute(
                "SELECT state FROM requests WHERE request_id = 'req-001'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(state, "unknown")

    def test_completed_duplicate_returns_cached_response(self):
        response = self.proxy.UpstreamResponse(200, "OK", {}, b'{"choices":[]}')
        with mock.patch.object(self.proxy, "forward_once", return_value=response) as forward:
            first = self.forward()
            second = self.forward()

        self.assertEqual(forward.call_count, 1)
        self.assertEqual(second.body, first.body)
        self.assertEqual(second.headers["X-Request-Ledger"], "cached")

    def test_unknown_duplicate_is_rejected_without_resend(self):
        self.ledger.claim(
            "req-001",
            self.proxy.request_payload_hash("POST", "/v1/chat/completions", self.body),
        )
        self.ledger.mark("req-001", "unknown", "primary")

        with mock.patch.object(self.proxy, "forward_once") as forward:
            with self.assertRaises(self.proxy.RequestLedgerBusy):
                self.forward()

        forward.assert_not_called()

    def test_request_id_payload_mismatch_is_rejected(self):
        self.ledger.claim(
            "req-001",
            self.proxy.request_payload_hash("POST", "/v1/chat/completions", b"first"),
        )

        with mock.patch.object(self.proxy, "forward_once") as forward:
            with self.assertRaises(self.proxy.RequestLedgerConflict):
                self.forward()

        forward.assert_not_called()

    def test_missing_request_id_is_rejected_before_upstream(self):
        with mock.patch.object(self.proxy, "forward_once") as forward:
            with self.assertRaises(self.proxy.RequestLedgerConflict):
                self.proxy.forward_with_failover(
                    "POST",
                    "/v1/chat/completions",
                    {},
                    self.body,
                    self.config,
                    self.ledger,
                )

        forward.assert_not_called()

    def test_primary_and_standby_share_one_timeout_budget(self):
        config = self.proxy.parse_config(
            {
                "WEBDOCK_FAILOVER_LEDGER_PATH": self.ledger_path,
                "WEBDOCK_FAILOVER_UPSTREAM_TIMEOUT_SECONDS": "1",
            }
        )
        timeouts = []

        def fake_forward(method, path, headers, body, host, port, timeout, on_connected=None):
            timeouts.append(timeout)
            if callable(on_connected):
                on_connected()
            if port == config.primary_port:
                import time

                time.sleep(0.02)
                return self.proxy.UpstreamResponse(
                    503,
                    "Service Unavailable",
                    {},
                    b'{"detail":{"message":"CDP attach failed"}}',
                )
            return self.proxy.UpstreamResponse(200, "OK", {}, b"{}")

        with mock.patch.object(self.proxy, "forward_once", side_effect=fake_forward):
            self.proxy.forward_with_failover(
                "POST",
                "/v1/chat/completions",
                self.headers,
                self.body,
                config,
                self.ledger,
            )

        self.assertEqual(len(timeouts), 2)
        self.assertLess(timeouts[1], timeouts[0])


class ForwardOnceBoundaryTests(unittest.TestCase):
    def test_connect_error_is_known_pre_submit(self):
        proxy = load_proxy_module()
        connection = mock.Mock()
        connection.connect.side_effect = ConnectionRefusedError("refused")

        with mock.patch.object(proxy.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(proxy.PreSubmitConnectionError):
                proxy.forward_once("POST", "/", {}, b"{}", "127.0.0.1", 1, 1)

    def test_error_after_connect_is_delivery_unknown(self):
        proxy = load_proxy_module()
        connection = mock.Mock()
        connection.request.side_effect = ConnectionResetError("reset")

        with mock.patch.object(proxy.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(proxy.DeliveryUnknownError):
                proxy.forward_once("POST", "/", {}, b"{}", "127.0.0.1", 1, 1)


if __name__ == "__main__":
    unittest.main()
