from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = (ROOT / "services/backend-api/app/routers/couple.py").read_text()
SHARE_PAGE = (ROOT / "services/public-web/s/index.html").read_text()


def test_share_media_uses_token_scoped_proxy_routes() -> None:
    assert '@router.get("/v1/share/{token}/photos/{photo_id}")' in ROUTER
    assert '@router.get("/v1/share/{token}/immich-assets/{asset_id}/thumbnail")' in ROUTER
    assert '"thumbnail_url": f"/api/v1/share/{urllib.parse.quote(token, safe=\'\')}/photos/' in ROUTER
    assert '"thumbnail_url": f"/api/v1/share/{urllib.parse.quote(token, safe=\'\')}/immich-assets/' in ROUTER


def test_share_page_renders_local_and_immich_media() -> None:
    assert 'id="cover"' in SHARE_PAGE
    assert 'd.photos||[]' in SHARE_PAGE
    assert 'd.immich_assets||[]' in SHARE_PAGE
    assert 'a.display_url||a.thumbnail_url' in SHARE_PAGE
