import importlib.util
import pathlib
import sys
import unittest


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


if __name__ == "__main__":
    unittest.main()
