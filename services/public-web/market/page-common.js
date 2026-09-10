/* Shared login handoff and guarded API requests for the three split pages. */
(function () {
  "use strict";
  const keys = ["aliecs_auth_token", "portal_token", "admin_token"];
  const token = () => keys.map((key) => localStorage.getItem(key)).find(Boolean) || "";
  const clearToken = () => keys.forEach((key) => localStorage.removeItem(key));
  const b64 = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  async function login() {
    const verifier = b64(crypto.getRandomValues(new Uint8Array(32)));
    sessionStorage.setItem("market_handoff_verifier", verifier);
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    const challenge = b64(new Uint8Array(digest));
    const returnTo = `${location.origin}${location.pathname}${location.search}`;
    location.assign(`https://hydwang.xyz/api/v1/auth/oidc/login?rd=${encodeURIComponent(returnTo)}&handoff_challenge=${challenge}`);
  }
  async function absorbLoginHandoff() {
    const code = new URLSearchParams(location.hash.slice(1)).get("handoff_code");
    if (!location.hash) return false;
    history.replaceState(null, "", `${location.pathname}${location.search}`);
    if (!code) return false;
    const verifier = sessionStorage.getItem("market_handoff_verifier");
    sessionStorage.removeItem("market_handoff_verifier");
    if (!verifier) throw new Error("登录交接缺少本浏览器绑定，请重新登录。");
    try {
      const body = await request("/api/v1/auth/oidc/handoff", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({code, verifier}), auth: false, timeout: 8000,
      });
      if (!body?.token || typeof body.token !== "string") throw new Error("登录交接返回无效，请重新登录。");
      localStorage.setItem("aliecs_auth_token", body.token);
      return true;
    } catch (error) {
      if (error.cause === "timeout") throw new Error("登录交接超时，请重新登录。", {cause: "handoff-timeout"});
      throw new Error("登录交接已失效，请重新登录。", {cause: "handoff"});
    }
  }
  function errorFor(response, body) {
    if (response.status === 401) {
      clearToken();
      return new Error("登录已过期，请重新登录。", {cause: "login"});
    }
    if (response.status === 403) return new Error("当前账户没有市场查看权限。", {cause: "forbidden"});
    const error = new Error(body?.detail || `市场接口返回 HTTP ${response.status}`);
    const retryAfter = Number(response.headers.get("retry-after"));
    if (Number.isFinite(retryAfter) && retryAfter >= 0) error.retryAfterMs = Math.min(retryAfter * 1000, 30000);
    return error;
  }
  async function request(path, options = {}) {
    const controller = options.controller || new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), options.timeout || 8000);
    try {
      const currentToken = options.auth === false ? "" : token();
      const response = await fetch(path, {...options, signal: controller.signal, cache: "no-store", headers: {...(options.headers || {}), ...(currentToken ? {Authorization: `Bearer ${currentToken}`} : {})}});
      const type = response.headers.get("content-type") || "";
      const body = type.includes("application/json") ? await response.json() : null;
      if (!response.ok) throw errorFor(response, body);
      if (!body) throw new Error("市场接口返回了登录页面或非 JSON 内容。", {cause: "html"});
      return body;
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("市场接口请求超时。", {cause: "timeout"});
      throw error;
    } finally { window.clearTimeout(timeout); }
  }
  window.MarketPage = {token, clearToken, login, absorbLoginHandoff, request};
}());
