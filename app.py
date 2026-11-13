import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent import PokerAgent, RemoteError
from poker_math import equity
from prompts import STYLES
from storage import Store

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.local.json"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_url: str = "https://poker.example.com"
    model_url: str = "http://127.0.0.1:8000/v1"
    proxy_url: str = "http://127.0.0.1:7897"
    api_key: str = ""
    model: str = Field(default="your-model-id", min_length=1, max_length=200)
    username: str = Field(default="agent", min_length=1, max_length=100)
    password: str = ""
    style: str = "balanced"
    mix_weights: dict[str, float] = Field(default_factory=lambda: {"tag": 35, "lag": 20, "balanced": 30, "exploit": 15})
    custom_prompt: str = Field(default="", max_length=1200)
    social: bool = True

    @field_validator("server_url", "model_url", "proxy_url")
    @classmethod
    def url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username \
                or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("地址必须是完整的 HTTP(S) URL，不能包含凭据、查询参数或片段")
        return value.rstrip("/")

    @model_validator(mode="after")
    def styles(self):
        if self.style not in {*STYLES, "mixed"}:
            raise ValueError("未知风格")
        if any(k not in STYLES or not 0 <= v <= 100 for k, v in self.mix_weights.items()):
            raise ValueError("混合权重需在 0–100，且风格名称有效")
        if self.style == "mixed" and not sum(self.mix_weights.values()):
            raise ValueError("混合风格至少需要一个正权重")
        return self


def read_settings():
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    for env, key in (("POKER_PASSWORD", "password"), ("POKER_MODEL_KEY", "api_key")):
        if os.environ.get(env):
            data[key] = os.environ[env]
    return Settings(**data).model_dump()


@asynccontextmanager
async def lifespan(application):
    store = Store(ROOT / "data" / "agent.sqlite3")
    application.state.agent = PokerAgent(read_settings(), store)
    application.state.lock = asyncio.Lock()
    yield
    await application.state.agent.close()
    store.db.close()


app = FastAPI(title="HPoker-Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = urlsplit("http://" + request.headers.get("host", "")).hostname
    origin = request.headers.get("origin")
    if host not in ("127.0.0.1", "localhost", "::1", "testserver"):
        return JSONResponse({"detail": "控制面板仅允许本机访问"}, status_code=403)
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        return JSONResponse({"detail": "不允许跨站访问控制接口"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
    return response


@app.exception_handler(RemoteError)
async def remote_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=exc.code if exc.code < 500 else 502)


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse({"detail": [{"loc": error["loc"], "msg": error["msg"]} for error in exc.errors()]},
                        status_code=422)


@app.exception_handler(httpx.HTTPError)
async def network_error(request, exc):
    return JSONResponse({"detail": "远端服务连接失败（" + type(exc).__name__ + "）"}, status_code=502)


def agent():
    return app.state.agent


def public_settings():
    data = dict(agent().settings)
    data["has_password"] = bool(data.pop("password"))
    data["has_api_key"] = bool(data.pop("api_key"))
    return {"settings": data, "styles": STYLES}


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/settings")
async def get_settings():
    return public_settings()


@app.put("/api/settings")
async def save_settings(settings: Settings):
    async with app.state.lock:
        active = agent()
        if active.running or active.stopping:
            raise HTTPException(409, "请先停止当前对局再修改配置")
        data = settings.model_dump()
        for key in ("password", "api_key"):
            if not data[key]:
                data[key] = active.settings[key]
        temp = CONFIG_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, CONFIG_PATH)
        if any(data[k] != active.settings[k] for k in ("username", "password", "server_url")):
            active.token = active.user = None
        active.settings = data
        await active.poker_http.aclose()
        active.poker_http = httpx.AsyncClient(proxy=data["proxy_url"], trust_env=False, timeout=15)
        active.profiles = active.store.profiles(data["server_url"])
        return public_settings()


@app.get("/api/models")
async def models():
    active = agent()
    response = await active.http.get(active.settings["model_url"] + "/models",
                                    headers={"Authorization": "Bearer " + active.settings["api_key"]})
    response.raise_for_status()
    return {"models": [m["id"] for m in response.json().get("data", [])]}


@app.post("/api/login")
async def login():
    async with app.state.lock:
        if agent().running or agent().stopping:
            raise HTTPException(409, "对局中不能切换登录会话")
        return {"user": await agent().login()}


@app.get("/api/rooms")
async def rooms():
    return await agent().api("GET", "/api/rooms")


class TestRoom(BaseModel):
    bots: int = Field(default=3, ge=1, le=5)


@app.post("/api/test-room")
async def test_room(options: TestRoom):
    async with app.state.lock:
        active = agent()
        if active.running or active.stopping:
            raise HTTPException(409, "请先停止当前对局")
        if not active.token:
            await active.login()
        room = await active.api("POST", "/api/rooms", json={
            "room_name": "AI Agent · 娱乐测试", "buyin_chips": 1000, "cash_value": 0,
            "small_blind": 5, "action_timeout": 30, "max_seats": 6})
        room_id = room["room_id"]
        try:
            for _ in range(options.bots):
                await active.api("POST", f"/api/rooms/{room_id}/test-bots")
        except Exception:
            active.log("error", message="测试房已创建，添加机器人失败", room_id=room_id)
            raise
        active.log("test_room", room_id=room_id, cash_value=0, bots=options.bots)
        return {"room_id": room_id}


class StartRequest(BaseModel):
    room_id: str = Field(min_length=1, max_length=80)


@app.post("/api/start")
async def start(request: StartRequest):
    async with app.state.lock:
        await agent().start(request.room_id)
        return agent().snapshot()


@app.post("/api/stop")
async def stop():
    async with app.state.lock:
        await agent().stop()
        return agent().snapshot()


@app.get("/api/status")
async def status():
    return agent().snapshot()


@app.get("/api/history")
async def history():
    return agent().store.history()


class Calculation(BaseModel):
    hole: list[str]
    board: list[str] = Field(default_factory=list)
    opponents: int = Field(default=1, ge=1, le=8)
    iterations: int = Field(default=600, ge=100, le=3000)


@app.post("/api/calculate")
async def calculate(request: Calculation):
    return await asyncio.to_thread(equity, request.hole, request.board, request.opponents, request.iterations)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8766)
