import os, sqlite3, requests, uvicorn
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# ---------- 配置 ----------
DB_PATH = Path(__file__).parent / "activity.db"
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "baobao521")
BARK_KEY = os.environ.get("BARK_API_KEY", "e4xKQoCEQ4fnzNW6UnqiBU")

# 北京时区偏移（用于判断"今天"）
BEIJING_OFFSET = timedelta(hours=8)

# ---------- 初始化数据库 ----------
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT NOT NULL,
            event TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    # ★ 新增：手机状态表 ★
    cur.execute("""
        CREATE TABLE IF NOT EXISTS device_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            battery TEXT, location TEXT, device TEXT,
            weather TEXT, brightness TEXT, volume TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------- FastAPI 应用 ----------
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ---------- 上报接口（快捷指令调用） ----------
@app.post("/report")
async def report(req: Request):
    auth = req.headers.get("Authorization", "")
    if AUTH_TOKEN and auth != f"Bearer {AUTH_TOKEN}":
        return {"status": "error", "message": "unauthorized"}
    data = await req.json()
    app_name = (data.get("app_name") or "").strip() or "未知App"
    event = data.get("event", "open")
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("INSERT INTO records (app_name, event, timestamp) VALUES (?,?,?)",
                (app_name, event, now))
    # ★ 新增：把手机状态也存进 device_state 表 ★
    battery = data.get("battery")
    location = data.get("location")
    device = data.get("device")
    weather = data.get("weather")
    brightness = data.get("brightness")
    volume = data.get("volume")
    if battery or location or device or weather or brightness or volume:
        cur.execute(
            "INSERT INTO device_state (battery, location, device, weather, brightness, volume, timestamp) VALUES (?,?,?,?,?,?,?)",
            (battery, location, device, weather, brightness, volume, now)
        )
    conn.commit()
    conn.close()
    return {"status": "ok", "received": {"app_name": app_name, "event": event}}

# ---------- 汇总接口（MCP 查询用）----------
@app.get("/activity/summary")
async def summary():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # ★ 新增：只统计"今天"（北京时间）的记录 ★
    beijing_now = datetime.utcnow() + BEIJING_OFFSET
    today_str = beijing_now.date().isoformat()   # 例如 "2025-01-15"

    # 最近打开（只看今天的最近5条）
    cur.execute(
        "SELECT app_name, event, timestamp FROM records "
        "WHERE date(timestamp, '+8 hours') = ? ORDER BY id DESC LIMIT 5",
        (today_str,)
    )
    recent = cur.fetchall()

    # 今天的所有记录，按时间正序（用于配对算时长）
    cur.execute(
        "SELECT app_name, event, timestamp FROM records "
        "WHERE date(timestamp, '+8 hours') = ? ORDER BY id ASC",
        (today_str,)
    )
    rows = cur.fetchall()
    conn.close()

    # 智能配对：打开进栈，关闭取最近一次打开配对（不要求名字完全一致）
    sessions, opens_stack = {}, []
    for r in rows:
        app, ev, ts = r
        if ev == "open":
            opens_stack.append((app, datetime.fromisoformat(ts)))
        elif ev == "close" and opens_stack:
            app_open, t_open = opens_stack.pop()
            gap = int((datetime.fromisoformat(ts) - t_open).total_seconds())
            sessions[app_open] = sessions.get(app_open, 0) + gap

    return {
        "recent_apps": [r[0] for r in recent],
        "sessions": sessions
    }

# ---------- ★ 新增：手机状态查询接口 ★ ----------
@app.get("/device/state")
async def device_state():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute(
        "SELECT battery, location, device, weather, brightness, volume, timestamp "
        "FROM device_state ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"state": None}
    return {"state": {
        "battery": row[0], "location": row[1], "device": row[2],
        "weather": row[3], "brightness": row[4], "volume": row[5],
        "timestamp": row[6]
    }}

# ---------- 本地运行 ----------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
