/**
 * 全站消息提示。原来各页把消息写在顶部内联横幅里，页面滚动后消息在视口外看不到；
 * 这里统一改成固定在视口内的浮层（桌面右上、移动端底部）。
 * 用法：AliECSToast.show('文字', 'success'|'error'|'warn')；AliECSToast.hide()；
 * 文字为空等同于 hide()。error 常驻直到手动关闭或被下一条覆盖，其余 6 秒自动消失。
 * admin-ui 是独立镜像（构建上下文只有 services/admin-ui），所以 services/admin-ui/common/toast.js
 * 是本文件的副本；改这里必须同步改那里，tests/test_frontend_toast.py 会断言两份完全一致。
 */
window.AliECSToast = (() => {
  const AUTO_HIDE_MS = 6000;
  const CSS = `
.aliecs-toast{position:fixed;z-index:2147483000;top:16px;right:16px;display:none;gap:10px;align-items:flex-start;
  box-sizing:border-box;max-width:min(420px,calc(100vw - 32px));max-height:60vh;overflow:auto;
  padding:12px 14px;border:1px solid #e7dfd0;border-left:4px solid #a46b1f;border-radius:12px;
  background:#fffdf8;color:#25211b;box-shadow:0 12px 30px rgba(69,56,38,.22);
  font-size:14px;line-height:1.5;text-align:left;white-space:pre-wrap;word-break:break-word}
.aliecs-toast.show{display:flex;animation:aliecs-toast-in .18s ease-out}
.aliecs-toast.success{border-left-color:#3f7a4c}
.aliecs-toast.warn{border-left-color:#a46b1f}
.aliecs-toast.error{border-left-color:#a6423a}
.aliecs-toast-text{flex:1 1 auto;min-width:0}
.aliecs-toast-close{flex:0 0 auto;min-width:0;min-height:0;margin:-2px -4px 0 0;padding:0 4px;border:0;border-radius:6px;
  background:transparent;color:#766f63;font-size:18px;font-weight:600;line-height:1.2;cursor:pointer}
@keyframes aliecs-toast-in{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
@media(max-width:600px){
  .aliecs-toast{top:auto;left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom));max-width:none}
  @keyframes aliecs-toast-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
}
@media(prefers-reduced-motion:reduce){.aliecs-toast.show{animation:none}}
`;

  let node = null;
  let timer = null;

  function mount() {
    if (node) return node;
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
    node = document.createElement("div");
    node.className = "aliecs-toast";
    node.setAttribute("role", "status");
    node.setAttribute("aria-live", "polite");
    const text = document.createElement("span");
    text.className = "aliecs-toast-text";
    const close = document.createElement("button");
    close.className = "aliecs-toast-close";
    close.type = "button";
    close.setAttribute("aria-label", "关闭");
    close.textContent = "×";
    close.onclick = hide;
    node.append(text, close);
    document.body.appendChild(node);
    return node;
  }

  function hide() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (node) node.classList.remove("show");
  }

  function show(text, type = "error") {
    const message = String(text ?? "");
    if (!message) { hide(); return; }
    if (!document.body) {
      document.addEventListener("DOMContentLoaded", () => show(message, type), { once: true });
      return;
    }
    const kind = type === "success" || type === "good" ? "success" : type === "warn" ? "warn" : "error";
    const box = mount();
    box.querySelector(".aliecs-toast-text").textContent = message;
    box.className = `aliecs-toast ${kind} show`;
    if (timer) { clearTimeout(timer); timer = null; }
    if (kind !== "error") timer = setTimeout(hide, AUTO_HIDE_MS);
  }

  return { show, hide };
})();
