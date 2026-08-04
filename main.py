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

# ---------- ★ 修复版：编码翻译官 ★ ----------
# iOS 快捷指令发中文时，UTF-8 字节被当成 GBK 读，产生乱码。
# 修复：把乱码按 GBK 编码还原成 UTF-8 字节再解码。
# errors="ignore" 是关键：遇到 � 等坏字符直接跳过，不整段放弃。
GARBLED_MARKERS = "锛鏄姘閮澶鐨涓鏍鍦浜鍏鍦板樊鎬т笂涓嬪墠鍚庡乏鍙"

def _looks_garbled(text):
    return any(ch in text for ch in GARBLED_MARKERS)

def repair_encoding(text):
    if not isinstance(text, str) or not text:
        return text
    if not _looks_garbled(text):
        return text
    try:
        repaired = text.encode("gbk", errors="ignore").decode("utf-8", errors="ignore")
        if repaired and "\ufffd" not in repaired:
            return repaired
    except Exception:
        pass
    return text

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
    app_name = repair_encoding((data.get("app_name") or "").strip() or "未知App")
    event = data.get("event", "open")
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("INSERT INTO records (app_name, event, timestamp) VALUES (?,?,?)",
                (app_name, event, now))
    battery = repair_encoding(data.get("battery"))
    location = repair_encoding(data.get("location"))
    device = repair_encoding(data.get("device"))
    weather = repair_encoding(data.get("weather"))
    brightness = repair_encoding(data.get("brightness"))
    volume = repair_encoding(data.get("volume"))
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
    beijing_now = datetime.utcnow() + BEIJING_OFFSET
    today_str = beijing_now.date().isoformat()
    cur.execute(
        "SELECT app_name, event, timestamp FROM records "
        "WHERE date(timestamp, '+8 hours') = ? ORDER BY id DESC LIMIT 5",
        (today_str,)
    )
    recent = cur.fetchall()
    cur.execute(
        "SELECT app_name, event, timestamp FROM records "
        "WHERE date(timestamp, '+8 hours') = ? ORDER BY id ASC",
        (today_str,)
    )
    rows = cur.fetchall()
    conn.close()
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

# ---------- ★ 新增：自测接口（先验证翻译官，再跑快捷指令） ----------
@app.get("/debug/repair")
async def debug_repair(text: str = ""):
    return {"original": text, "repaired": repair_encoding(text)}

# ---------- 手机状态查询接口 ----------
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
