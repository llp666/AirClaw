"""演示模式的三道闸 —— 把系统交给别人体验时的最小防护。

单用户本机部署不需要这些。可一旦要让外部访客访问（公网 IP + 开放端口），现状是
**完全没有防护**的：没有登录、访客能删会话/删语料/改 System Prompt、没有任何用量上限。
本模块补上三件事：

  1. 共享口令    除 /api/health 外，所有请求（含静态界面）要求 HTTP Basic 认证。
                 挡的是「链接被转发出去」这类风险，不是账号体系。
  2. 只读        除「发起对话」与「新建会话」外，一切非 GET 请求一律拒绝。
                 Agent 照常读文件、跑沙箱（那是它的本职），但访客改不了任何东西。
  3. 按 IP 限流  对话条数（滚动一小时）与单条消息长度封顶，避免有人拿它刷模型额度。
                 单条长度在 api/chat.py 里校验——那里才拿得到消息正文。

另有一层不在本模块：演示模式下会话按访客 IP 隔离（见 api/sessions.py 的 _own），
访客互相看不到对方的对话。

为什么写成**纯 ASGI 中间件**而不是 `BaseHTTPMiddleware` 或 `@app.middleware("http")`：
那两个都走 BaseHTTPMiddleware，会包一层响应缓冲，而对话接口是 SSE 长连接——已知会
影响流式输出。纯 ASGI 只做判断后透传，不碰响应体。

**口令走 HTTP Basic，明文 HTTP 下就是明文传输。** 要让链路上的人看不到它，前面必须
有 TLS（域名 + 证书，或反向代理）。没有 TLS 时，这个口令只挡「链接外传」，不挡抓包。
"""

from __future__ import annotations

import base64
import hmac
import time
from collections import defaultdict

from fastapi import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from config import Settings

#: 不需要口令的路径。容器 HEALTHCHECK 打的就是它，带上认证反而会一直不健康
EXEMPT_PATHS = frozenset({"/api/health"})

#: 演示模式下仍然放行的写操作：发起对话、新建会话。
#: 其余非 GET 请求（保存文件、上传技能、语料入库/删除、删会话、切 RAG、压缩历史）一律拒绝。
ALLOWED_WRITES = frozenset({("POST", "/api/chat"), ("POST", "/api/sessions")})

RATE_WINDOW_SECONDS = 3600


def visitor_id(request: Request) -> str:
    """访客标识，演示模式下用于隔离会话。

    取客户端 IP 而不引入登录体系：访客只需一个口令，不用注册。代价是换网络（或移动网络
    换基站）会认不出是同一个人——演示场景可以接受。
    """
    return request.client.host if request.client else "unknown"


class DemoGuard:
    """口令 / 只读 / 限流。只在 settings.demo_mode 打开时装到应用上。"""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._settings = settings
        #: 访客 IP → 本窗口内的对话时间戳。只存进程内存，重启即清空——
        #: 演示场景够用，也免得为此引入 Redis 之类的依赖。
        self._hits: dict[str, list[float]] = defaultdict(list)

    # ---- 三道闸 ----

    def _password_ok(self, scope: Scope) -> bool:
        configured = self._settings.demo_password
        if not configured:
            # 开了演示模式却没设口令：一律拒绝，而不是静默变成一个公开服务
            return False
        raw = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        if not raw.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(raw[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _, _, password = decoded.partition(":")
        # 对访客只发一个口令，用户名不校验；定时比较，避免逐字符试探的时序侧信道
        return hmac.compare_digest(password, configured)

    def _rate_limited(self, client: str, now: float) -> bool:
        limit = self._settings.demo_chat_per_hour
        if limit <= 0:
            return False
        recent = [t for t in self._hits[client] if now - t < RATE_WINDOW_SECONDS]
        if len(recent) >= limit:
            self._hits[client] = recent
            return True
        recent.append(now)
        self._hits[client] = recent
        return False

    # ---- ASGI ----

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"]
        method = scope["method"].upper()
        client = scope["client"][0] if scope.get("client") else "unknown"

        # OPTIONS 是 CORS 预检，浏览器不会带凭证，拦它等于把跨域全堵死
        if path not in EXEMPT_PATHS and method != "OPTIONS":
            if not self._password_ok(scope):
                return await self._deny(
                    scope, receive, send, 401, "需要访问口令。",
                    {"WWW-Authenticate": 'Basic realm="AirClaw"'},
                )
            if method not in ("GET", "HEAD") and (method, path) not in ALLOWED_WRITES:
                return await self._deny(
                    scope, receive, send, 403,
                    "演示模式为只读：可以对话，但不能修改文件、技能、语料或会话。",
                )
            if path == "/api/chat" and self._rate_limited(client, time.time()):
                return await self._deny(
                    scope, receive, send, 429,
                    f"演示模式限流：每小时最多 {self._settings.demo_chat_per_hour} 次对话，已用满。",
                )

        return await self.app(scope, receive, send)

    @staticmethod
    async def _deny(
        scope: Scope, receive: Receive, send: Send,
        status: int, message: str, headers: dict[str, str] | None = None,
    ) -> None:
        response = PlainTextResponse(message, status_code=status, headers=headers or {})
        await response(scope, receive, send)
