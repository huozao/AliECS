window.AliECSAuth = (() => {
  const API_BASE = location.port === "8080" ? "http://localhost:8000" : "/api";
  const AUTH_KEYS = ["aliecs_auth_token", "portal_token", "admin_token"];

  function getToken() {
    return AUTH_KEYS.map((k) => localStorage.getItem(k) || "").find(Boolean) || "";
  }

  async function fetchMe() {
    const token = getToken();
    if (!token) return null;
    const resp = await fetch(`${API_BASE}/v1/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) return null;
    return resp.json();
  }

  async function renderUserBadge(elId = "userBadge", prefix = "当前登录：") {
    const el = document.getElementById(elId);
    if (!el) return null;
    try {
      const me = await fetchMe();
      if (!me || !me.sub) return null;
      el.textContent = `${prefix}${me.display_name ? `${me.display_name}（${me.sub}）` : me.sub}`;
      el.classList.remove("hidden");
      return me;
    } catch {
      return null;
    }
  }

  return { API_BASE, getToken, fetchMe, renderUserBadge };
})();
