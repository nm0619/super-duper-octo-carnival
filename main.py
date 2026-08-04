import os, sqlite3, requests, uvicorn, base64, json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

# ---------- 配置 ----------
DB_PATH = Path(__file__).parent / "activity.db"
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "baobao521")
BARK_KEY = os.environ.get("BARK_API_KEY", "e4xKQoCEQ4fnzNW6UnqiBU")
BEIJING_OFFSET = timedelta(hours=8)

# ---------- 编码处理（三保险：Base64 → URL → 乱码修复） ----------
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

def clean_field(value):
    if value is None:
        return None
    value = str(value).strip()
    if value and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in value):
        try:
            decoded = base64.b64decode(value).decode("utf-8")
            if decoded and any(ord(ch) > 127 for ch in decoded):
                return decoded
        except Exception:
            pass
    try:
        value = unquote(value)
    except Exception:
        pass
    return repair_encoding(value)

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

# ---------- 上报接口 ----------
@app.post("/report")
async def report(req: Request):
    auth = req.headers.get("Authorization", "")
    if AUTH_TOKEN and auth != f"Bearer {AUTH_TOKEN}":
        return {"status": "error", "message": "unauthorized"}
    data = await req.json()
    print("📱 收到上报原始数据:", json.dumps(data, ensure_ascii=False)[:500])
    app_name = clean_field(data.get("app_name")) or "未知App"
    event = data.get("event", "open")
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("INSERT INTO records (app_name, event, timestamp) VALUES (?,?,?)",
                (app_name, event, now))
    battery = clean_field(data.get("battery"))
    location = clean_field(data.get("location"))
    device = clean_field(data.get("device"))
    weather = clean_field(data.get("weather"))
    brightness = clean_field(data.get("brightness"))
    volume = clean_field(data.get("volume"))
    print("🧹 清洗后:", json.dumps({"battery": battery, "location": location, "device": device, "weather": weather}, ensure_ascii=False)[:500])
    if battery or location or device or weather or brightness or volume:
        cur.execute(
            "INSERT INTO device_state (battery, location, device, weather, brightness, volume, timestamp) VALUES (?,?,?,?,?,?,?)",
            (battery, location, device, weather, brightness, volume, now)
        )
    conn.commit()
    conn.close()
    return {"status": "ok", "received": {"app_name": app_name, "event": event}}

# ---------- 汇总接口 ----------
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

# ---------- 手机状态查询接口（中文原文 + 显式声明 UTF-8，浏览器不再乱码） ----------
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
    cleaned = {
        "battery": clean_field(row[0]),
        "location": clean_field(row[1]),
        "device": clean_field(row[2]),
        "weather": clean_field(row[3]),
        "brightness": clean_field(row[4]),
        "volume": clean_field(row[5]),
        "timestamp": row[6]
    }
    print("📖 读取并清洗:", json.dumps(cleaned, ensure_ascii=False)[:500])
    loc = cleaned.get("location") or ""
    codes = [ord(ch) for ch in loc[:20]]
    payload = {"state": cleaned, "debug_location_codes": codes}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # ★ 关键：显式声明 charset=utf-8，告诉浏览器按 UTF-8 解码 ★
    return Response(content=body, media_type="application/json; charset=utf-8")

# ---------- 本地运行 ----------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
