from __future__ import annotations

import hmac

from mcp.server.auth.provider import AuthorizationParams
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .config import OAuthConfig
from .provider import AliecsOAuthProvider

_FORM = """<!doctype html><html><head><meta charset="utf-8"><title>AliECS Coding 授权</title></head>
<body style="font-family:sans-serif;max-width:420px;margin:64px auto">
<h3>AliECS Coding 连接授权</h3>
<p>请输入授权口令以允许 ChatGPT 连接。</p>
{error}
<form method="post" action="/oauth/consent">
<input type="hidden" name="txn" value="{txn}">
<input type="password" name="passphrase" autofocus style="width:100%;padding:8px;font-size:16px" placeholder="口令">
<button type="submit" style="margin-top:12px;padding:8px 16px">授权</button>
</form></body></html>"""


def make_consent_handler(provider: AliecsOAuthProvider, config: OAuthConfig):
    async def consent(request: Request):
        if request.method == "GET":
            txn = request.query_params.get("txn", "")
            return HTMLResponse(_FORM.format(txn=_esc(txn), error=""))

        form = await request.form()
        txn = str(form.get("txn", ""))
        passphrase = str(form.get("passphrase", ""))
        if not hmac.compare_digest(passphrase, config.passphrase):
            return HTMLResponse(
                _FORM.format(txn=_esc(txn), error='<p style="color:#c00">口令错误</p>'),
                status_code=403,
            )
        pending = provider.store.take_pending(txn)
        if pending is None:
            return HTMLResponse("<p>授权请求已过期，请在 ChatGPT 重新连接。</p>", status_code=400)
        client_id, params_json = pending
        params = AuthorizationParams.model_validate_json(params_json)
        redirect = provider.complete_authorization(client_id, params)
        return RedirectResponse(redirect, status_code=302)

    return consent


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
