from pathlib import Path


def test_public_homepage_has_required_icp_filing_link():
    html = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "public-web"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert "蜀ICP备2026040494号-2" in html
    assert 'href="https://beian.miit.gov.cn/"' in html
    assert "<title>材色智配</title>" in html
    assert "<h1>材色智配</h1>" in html
