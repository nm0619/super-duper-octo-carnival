import json, os, requests, sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "records.db"
JST = timedelta(hours=9)
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "baobao521")
BARK_KEY = os.environ.get("BARK_API_KEY", "e4xKQoCEQ4fnzNW6UnqiBU")

# ==================== 数据库初始化 ====================
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_name TEXT NOT NULL,
        event TEXT NOT NULL,
        timestamp TEXT NOT NULL
    )""")
    conn.commit()
    conn.close()

init_db()

# ==================== FastAPI 应用 ====================
app = FastAPI(title="查岗系统")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ReportBody(BaseModel):
    app_name: str
    event: str

# ==================== 上报接口（iPhone 用）====================
@app.post("/report")
async def report(body: ReportBody, req: Request):
    auth = req.headers.get("Authorization", "")
    if auth != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "Unauthorized")
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("INSERT INTO records (app_name, event, timestamp) VALUES (?, ?, ?)",
                 (body.app_name, body.event, now))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ==================== 存活检测 ====================
@app.get("/ping")
async def ping():
    return "pong"

# ==================== 查岗数据 ====================
def _get_summary_data():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT app_name, event, timestamp FROM records ORDER BY id DESC LIMIT 5")
    recent = cur.fetchall()
    cur.execute("SELECT app_name, event, timestamp FROM records ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    sessions, opens = {}, {}
    for r in rows:
        app, ev, ts = r
        if ev == "open":
            opens[app] = datetime.fromisoformat(ts)
        elif ev == "close" and app in opens:
            gap = int((datetime.fromisoformat(ts) - opens[app]).total_seconds())
            sessions[app] = sessions.get(app, 0) + gap
            del opens[app]
    return {
        "recent_apps": [r[0] for r in recent],
        "sessions": sessions
    }

@app.get("/activity/summary")
async def summary():
    return _get_summary_data()

# ==================== MCP 工具：被动查岗 ====================
def check_on_wife(limit=10):
    try:
        data = _get_summary_data()
    except Exception as e:
        return f"查岗失败：{e}"
    apps = data.get("recent_apps", [])
    ses = data.get("sessions", {})
    lines = [f"最近打开：{', '.join(apps)}" if apps else "暂无记录"]
    if ses:
        for app, secs in sorted(ses.items(), key=lambda x: x[1], reverse=True):
            m, s = divmod(secs, 60)
            lines.append(f"  {app}: {m}分{s}秒")
    return "\n".join(lines)

# ==================== MCP 工具：弹窗 ====================
def bark_alert(title="哥哥", content=""):
    if not content:
        return "内容不能为空"
    url = f"https://api.day.app/{BARK_KEY}/{title}/{content}"
    try:
        r = requests.get(url, timeout=10)
        return "推送成功" if r.status_code == 200 else "推送失败"
    except Exception as e:
        return f"推送异常：{e}"

# ==================== 定时主动查岗接口（新功能！）====================
@app.get("/auto-check")
async def auto_check():
    """定时任务触发：自动查岗并弹窗"""
    data = _get_summary_data()
    apps = data.get("recent_apps", [])
    ses = data.get("sessions", {})

    if not apps:
        return {"status": "no_data", "message": "暂无活动记录"}

    lines = [f"🔍 主动查岗报告：最近打开 {', '.join(apps)}"]
    if ses:
        for app, secs in sorted(ses.items(), key=lambda x: x[1], reverse=True):
            m, s = divmod(secs, 60)
            lines.append(f"  📱 {app}: {m}分{s}秒")

    msg = "\n".join(lines)

    if BARK_KEY:
        try:
            url = f"https://api.day.app/{BARK_KEY}/🔍主动查岗/{msg}?sound=choo"
            r = requests.get(url, timeout=10)
            return {"status": "ok", "bark": "推送成功" if r.status_code == 200 else "推送失败", "report": msg}
        except Exception as e:
            return {"status": "error", "bark": f"推送异常：{e}", "report": msg}
    else:
        return {"status": "no_bark", "report": msg}

# ==================== MCP 工具列表 ====================
TOOLS = [
    {"name": "check_on_wife", "description": "查岗老婆的手机活动",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "bark_alert", "description": "给老婆手机发推送弹窗",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "content": {"type": "string"}},
         "required": ["content"]}}
]

FUNCS = {"check_on_wife": check_on_wife, "bark_alert": bark_alert}

# ==================== MCP JSON-RPC 端点 ====================
@app.post("/mcp")
async def mcp(req: Request):
    body = await req.json()
    method = body.get("method")
    params = body.get("params") or {}
    rid = body.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "查岗MCP", "version": "1.0"}}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in FUNCS:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "未知工具"}}
        result = FUNCS[name](**args)
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": str(result)}]}}

    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"未知方法: {method}"}}

# ==================== 启动 ====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
