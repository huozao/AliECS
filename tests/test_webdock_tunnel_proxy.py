import importlib.util
import pathlib
import sys
import unittest


def load_proxy_module():
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "deploy" / "ecs" / "webdock-tunnel-proxy.py"
    spec = importlib.util.spec_from_file_location("webdock_tunnel_proxy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WebDockTunnelProxyConfigTests(unittest.TestCase):
    def test_defaults_bridge_docker_host_to_loopback_tunnel(self):
        proxy = load_proxy_module()
        config = proxy.parse_config({})

        self.assertEqual(config.bind_host, "172.17.0.1")
        self.assertEqual(config.bind_port, 11800)
        self.assertEqual(config.target_host, "127.0.0.1")
        self.assertEqual(config.target_port, 11800)

    def test_env_overrides_ports_and_hosts(self):
        proxy = load_proxy_module()
        config = proxy.parse_config(
            {
                "WEBDOCK_PROXY_BIND_HOST": "172.18.0.1",
                "WEBDOCK_PROXY_BIND_PORT": "11801",
                "WEBDOCK_PROXY_TARGET_HOST": "127.0.0.2",
                "WEBDOCK_PROXY_TARGET_PORT": "18000",
            }
        )

        self.assertEqual(config.bind_host, "172.18.0.1")
        self.assertEqual(config.bind_port, 11801)
        self.assertEqual(config.target_host, "127.0.0.2")
        self.assertEqual(config.target_port, 18000)


if __name__ == "__main__":
    unittest.main()
