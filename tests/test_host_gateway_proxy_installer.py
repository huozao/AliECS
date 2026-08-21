import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class HostGatewayProxyInstallerTests(unittest.TestCase):
    def test_webdock_storage_forces_the_rollback_compatible_11800_proxy(self):
        deploy = (ROOT / "deploy" / "ecs" / "deploy.sh").read_text(encoding="utf-8")
        expected_policy = (
            '&& ( "$WEBDOCK_TUNNEL_PROXY_ENABLED" == "true" \\\n'
            '      || "$STORAGE_DRIVER" == "webdock" )'
        )

        self.assertIn(expected_policy, deploy)
        self.assertIn(
            'bash "$ROOT_DIR/install-host-gateway-proxies.sh"',
            deploy,
            "deployment must not depend on a Windows checkout preserving +x",
        )

    def test_renders_named_loopback_proxy_instances(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            env = os.environ.copy()
            env["DESTDIR"] = temp_dir
            env["WSLENV"] = "DESTDIR/p"
            subprocess.run(
                ["bash", "deploy/ecs/install-host-gateway-proxies.sh"],
                cwd=ROOT,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            destination = pathlib.Path(temp_dir)
            unit = (
                destination
                / "etc"
                / "systemd"
                / "system"
                / "host-gateway-proxy@.service"
            ).read_text(encoding="utf-8")
            self.assertIn(
                "EnvironmentFile=/etc/default/host-gateway-proxy-%i", unit
            )
            self.assertIn(
                "ExecStart=/usr/bin/python3 /opt/aliecs/webdock-tunnel-proxy.py",
                unit,
            )

            expected_ports = {
                "wecom-kf": 18080,
                "erpnext": 18200,
                "paperless": 18201,
            }
            for name, port in expected_ports.items():
                rendered = (
                    destination / "etc" / "default" / f"host-gateway-proxy-{name}"
                ).read_text(encoding="utf-8")
                self.assertEqual(
                    rendered,
                    "\n".join(
                        [
                            "WEBDOCK_PROXY_BIND_HOST=172.17.0.1",
                            f"WEBDOCK_PROXY_BIND_PORT={port}",
                            "WEBDOCK_PROXY_TARGET_HOST=127.0.0.1",
                            f"WEBDOCK_PROXY_TARGET_PORT={port}",
                            "WEBDOCK_PROXY_BACKLOG=64",
                            "",
                        ]
                    ),
                )

            self.assertFalse(
                (
                    destination
                    / "etc"
                    / "default"
                    / "host-gateway-proxy-webdock-photo"
                ).exists(),
                "11800 stays on the rollback-compatible webdock-tunnel-proxy.service",
            )
            self.assertTrue(
                (destination / "opt" / "aliecs" / "webdock-tunnel-proxy.py").is_file()
            )


if __name__ == "__main__":
    unittest.main()
