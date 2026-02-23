"""
OpenKaguya 管理面板 — aiohttp Web Server。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path

from aiohttp import web
from loguru import logger

from kaguya.admin.api import create_api_routes
from kaguya.memory.database import Database

_STATIC_DIR = Path(__file__).parent / "static"


def _make_token(password: str, salt: str) -> str:
    """用密码和 salt 生成 session token"""
    return hmac.new(
        salt.encode(), password.encode(), hashlib.sha256
    ).hexdigest()


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """
    简单的密码认证中间件。

    - 无密码配置 → 全部放行
    - 有密码 → 检查 cookie 中的 session token
    - 登录页和登录 API 不做拦截
    """
    password = request.app.get("_admin_password", "")
    # 无密码 → 全部放行
    if not password:
        return await handler(request)

    # 放行登录相关路由
    path = request.path
    if path in ("/login", "/api/login"):
        return await handler(request)
    # 放行静态资源
    if path.startswith("/static/"):
        return await handler(request)

    # 检查 session cookie
    token = request.cookies.get("kaguya_session", "")
    expected = _make_token(password, request.app["_salt"])
    if hmac.compare_digest(token, expected):
        return await handler(request)

    # 未认证 → API 返回 401，页面重定向到登录
    if path.startswith("/api/"):
        return web.json_response({"error": "未认证"}, status=401)
    raise web.HTTPFound("/login")


async def start_admin_server(
    db: Database,
    host: str = "127.0.0.1",
    port: int = 8080,
    password: str = "",
) -> web.AppRunner:
    """
    启动管理面板 HTTP 服务器。

    Returns:
        AppRunner 实例（用于后续关闭）
    """
    salt = secrets.token_hex(16)

    app = web.Application(middlewares=[auth_middleware])
    app["_admin_password"] = password
    app["_salt"] = salt

    # 注册 API 路由
    api_routes = create_api_routes(db)
    app.router.add_routes(api_routes)

    # 登录 API
    async def login_handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            pwd = body.get("password", "")
        except Exception:
            pwd = ""
        if pwd == password:
            token = _make_token(password, salt)
            resp = web.json_response({"ok": True})
            resp.set_cookie("kaguya_session", token, httponly=True, samesite="Lax")
            return resp
        return web.json_response({"error": "密码错误"}, status=403)

    app.router.add_post("/api/login", login_handler)

    # 登录页
    async def login_page(request: web.Request) -> web.Response:
        html = _LOGIN_HTML.replace("{{TITLE}}", "🌙 OpenKaguya Admin")
        return web.Response(text=html, content_type="text/html")

    app.router.add_get("/login", login_page)

    # 静态文件
    app.router.add_static("/static", _STATIC_DIR)

    # 根路径
    async def index_handler(request: web.Request) -> web.FileResponse:
        return web.FileResponse(_STATIC_DIR / "index.html")

    app.router.add_get("/", index_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    if password:
        logger.info(f"🖥️  管理面板已启动: http://{host}:{port} (需要密码)")
    else:
        logger.info(f"🖥️  管理面板已启动: http://{host}:{port} (无密码保护)")
    return runner


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{TITLE}}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', sans-serif;
  background: #0b1120;
  color: #e8ecf1;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
}
.login-card {
  background: #1a2332;
  border: 1px solid #2a3544;
  border-radius: 16px;
  padding: 40px;
  width: 380px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.4);
}
.login-card h1 {
  text-align: center;
  font-size: 22px;
  margin-bottom: 8px;
  background: linear-gradient(135deg, #a78bfa, #60a5fa);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}
.login-card p {
  text-align: center;
  font-size: 13px;
  color: #5a6a7a;
  margin-bottom: 28px;
}
.input-group {
  margin-bottom: 20px;
}
.input-group label {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: #8899aa;
  margin-bottom: 6px;
}
.input-group input {
  width: 100%;
  padding: 10px 14px;
  background: #0f1923;
  border: 1px solid #2a3544;
  border-radius: 8px;
  color: #e8ecf1;
  font-size: 14px;
  outline: none;
}
.input-group input:focus { border-color: #8b5cf6; }
.btn-login {
  width: 100%;
  padding: 11px;
  background: linear-gradient(135deg, #7c3aed, #a855f7);
  border: none;
  border-radius: 8px;
  color: white;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-login:hover { opacity: 0.9; transform: translateY(-1px); }
.error-msg {
  text-align: center;
  color: #ec4899;
  font-size: 13px;
  margin-top: 12px;
  min-height: 20px;
}
</style>
</head>
<body>
<div class="login-card">
  <h1>🌙 OpenKaguya</h1>
  <p>管理面板登录</p>
  <form onsubmit="login(event)">
    <div class="input-group">
      <label>密码</label>
      <input type="password" id="pwd" autofocus placeholder="输入管理密码">
    </div>
    <button type="submit" class="btn-login">登录</button>
  </form>
  <div class="error-msg" id="err"></div>
</div>
<script>
async function login(e) {
  e.preventDefault();
  const pwd = document.getElementById('pwd').value;
  const res = await fetch('/api/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: pwd}),
  });
  const data = await res.json();
  if (data.ok) {
    window.location.href = '/';
  } else {
    document.getElementById('err').textContent = data.error || '登录失败';
  }
}
</script>
</body>
</html>"""
