from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backend_oidc_discovery_bypasses_public_cloudflare() -> None:
    compose = (ROOT / "deploy" / "ecs" / "compose.prod.yml").read_text(encoding="utf-8")

    assert '"auth.hydwang.xyz:host-gateway"' in compose
