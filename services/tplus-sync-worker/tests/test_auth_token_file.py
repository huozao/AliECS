import os
import tempfile
import unittest

from config.settings import Settings
from tplus_datahub.chanjet.auth import build_auth_headers, resolve_open_token


def _settings() -> Settings:
    return Settings(
        base_url="https://openapi.example.com",
        app_key="app-key",
        app_secret="app-secret",
        open_token="env-token",
        default_page_size=500,
        timeout_connect=5,
        timeout_read=30,
        output_dir="output",
        data_dir="data",
    )


class AuthTokenFileTests(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get("CHANJET_OPEN_TOKEN_FILE")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("CHANJET_OPEN_TOKEN_FILE", None)
        else:
            os.environ["CHANJET_OPEN_TOKEN_FILE"] = self._old_env

    def test_without_token_file_env_uses_settings_token(self):
        os.environ.pop("CHANJET_OPEN_TOKEN_FILE", None)
        self.assertEqual("env-token", resolve_open_token(_settings()))

    def test_token_file_overrides_settings_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("file-token\n")
            os.environ["CHANJET_OPEN_TOKEN_FILE"] = path
            self.assertEqual("file-token", resolve_open_token(_settings()))
            headers = build_auth_headers(_settings())
            self.assertEqual("file-token", headers["openToken"])

    def test_missing_or_empty_token_file_falls_back_to_settings_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CHANJET_OPEN_TOKEN_FILE"] = os.path.join(tmp, "absent.txt")
            self.assertEqual("env-token", resolve_open_token(_settings()))

            empty = os.path.join(tmp, "empty.txt")
            with open(empty, "w", encoding="utf-8"):
                pass
            os.environ["CHANJET_OPEN_TOKEN_FILE"] = empty
            self.assertEqual("env-token", resolve_open_token(_settings()))


if __name__ == "__main__":
    unittest.main()
