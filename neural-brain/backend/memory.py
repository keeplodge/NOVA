import aiosqlite
import json
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent / "brain.db"

CATEGORIES = {
    "trading":   {"color": "#00FFFF", "emoji": "📈"},
    "keeplodge": {"color": "#C9A84C", "emoji": "🏠"},
    "personal":  {"color": "#9B59B6", "emoji": "👤"},
    "ideas":     {"color": "#39FF14", "emoji": "💡"},
    "nova":      {"color": "#FF6B00", "emoji": "🤖"},
    "probuild":  {"color": "#4F86B8", "emoji": "🏗️"},
    "general":   {"color": "#4A90D9", "emoji": "🧠"},
}

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id          TEXT PRIMARY KEY,
                content     TEXT NOT NULL,
                summary     TEXT,
                category    TEXT DEFAULT 'general',
                tags        TEXT DEFAULT '[]',
                connections TEXT DEFAULT '[]',
                strength    REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 0,
                created_at  REAL,
                updated_at  REAL,
                x           REAL,
                y           REAL,
                z           REAL
            )
        """)
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
            USING fts5(content, summary, tags, content='memories', content_rowid='rowid')
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id         TEXT PRIMARY KEY,
                role       TEXT,
                content    TEXT,
                timestamp  REAL,
                memory_ids TEXT DEFAULT '[]'
            )
        """)
        await db.commit()

        # Seed with initial Nova memories if empty
        cur = await db.execute("SELECT COUNT(*) FROM memories")
        count = (await cur.fetchone())[0]
        if count == 0:
            await _seed_initial_memories(db)

async def _seed_initial_memories(db):
    import math, random
    seeds = [
        ("NOVA Trading System uses ICT concepts — liquidity sweeps, MSS, FVG, order blocks on NQ Futures and XAUUSD.", "NOVA trading system overview", "trading", ["ICT", "NQ", "XAUUSD", "strategy"]),
        ("Risk management: $500 fixed per trade, 2R target, breakeven at 1R. Max 3 trades per session.", "Trading risk rules", "trading", ["risk", "2R", "500"]),
        ("Sessions: London 2am-5am EST, NY AM 8:30am-11am EST, Asia 7pm-10pm EST.", "Trading sessions schedule", "trading", ["sessions", "London", "NYAM"]),
        ("TradingView alerts → Railway webhook → TradersPost → 3 live accounts. The full pipeline is confirmed working.", "Signal pipeline architecture", "trading", ["webhook", "Railway", "TradersPost"]),
        ("KeepLodge is a SaaS property management platform. Generates direct booking sites for STR hosts.", "KeepLodge platform overview", "keeplodge", ["SaaS", "STR", "booking"]),
        ("The Serenity Place — luxury 6-bed villa in Montego Bay, Jamaica. $950/night. Client site built.", "Serenity Place client", "keeplodge", ["villa", "Jamaica", "client"]),
        ("Nova Algo is the trading product brand. Landing page live at nova-algo.netlify.app", "Nova Algo product", "nova", ["product", "brand", "netlify"]),
        ("Hunnid Ticks is the trading community Discord.", "Hunnid Ticks community", "nova", ["discord", "community"]),
        ("NOVA Assistant runs daily at 8am with a full pre-flight briefing. Agents include trading, content, lead, and waitlist.", "Nova assistant schedule", "nova", ["assistant", "agents", "briefing"]),
    ]
    for content, summary, category, tags in seeds:
        mid = str(uuid.uuid4())[:8]
        # Spread nodes on sphere surface
        theta = random.uniform(0, 2 * math.pi)
        phi   = math.acos(random.uniform(-1, 1))
        r     = 2.5 + random.uniform(-0.3, 0.3)
        x = r * math.sin(phi) * math.cos(theta)
        y = r * math.sin(phi) * math.sin(theta)
        z = r * math.cos(phi)
        now = time.time()
        await db.execute("""
            INSERT INTO memories (id, content, summary, category, tags, created_at, updated_at, x, y, z)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (mid, content, summary, category, json.dumps(tags), now, now, x, y, z))
    await db.commit()

async def add_memory(content: str, category: str = "general", tags: list = None, summary: str = "") -> dict:
    import math, random
    mid = str(uuid.uuid4())[:8]
    tags = tags or []
    now  = time.time()
    theta = random.uniform(0, 2 * math.pi)
    phi   = math.acos(random.uniform(-1, 1))
    r     = 2.5 + random.uniform(-0.4, 0.4)
    x = r * math.sin(phi) * math.cos(theta)
    y = r * math.sin(phi) * math.sin(theta)
    z = r * math.cos(phi)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO memories (id, content, summary, category, tags, created_at, updated_at, x, y, z)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (mid, content, summary, category, json.dumps(tags), now, now, x, y, z))
        await db.commit()

    mem = {"id": mid, "content": content, "summary": summary, "category": category,
           "tags": tags, "x": x, "y": y, "z": z, "created_at": now}
    return mem

async def search_memories(query: str, limit: int = 8) -> list:
    results = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # FTS search
        try:
            cur = await db.execute("""
                SELECT m.* FROM memories m
                JOIN memories_fts f ON m.rowid = f.rowid
                WHERE memories_fts MATCH ?
                ORDER BY rank LIMIT ?
            """, (query, limit))
            rows = await cur.fetchall()
            results = [dict(r) for r in rows]
        except Exception:
            pass
        # Fallback LIKE search
        if not results:
            cur = await db.execute("""
                SELECT * FROM memories
                WHERE content LIKE ? OR summary LIKE ? OR tags LIKE ?
                ORDER BY updated_at DESC LIMIT ?
            """, (f"%{query}%", f"%{query}%", f"%{query}%", limit))
            rows = await cur.fetchall()
            results = [dict(r) for r in rows]

    for r in results:
        if isinstance(r.get("tags"), str):
            try: r["tags"] = json.loads(r["tags"])
            except: r["tags"] = []
    return results

async def get_all_memories() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM memories ORDER BY created_at DESC")
        rows = await cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("tags"), str):
            try: d["tags"] = json.loads(d["tags"])
            except: d["tags"] = []
        result.append(d)
    return result

async def get_recent_memories(limit: int = 20) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("tags"), str):
            try: d["tags"] = json.loads(d["tags"])
            except: d["tags"] = []
        result.append(d)
    return result

async def update_access(memory_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE memories SET access_count = access_count + 1, updated_at = ?
            WHERE id = ?
        """, (time.time(), memory_id))
        await db.commit()

async def save_conversation(role: str, content: str, memory_ids: list = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO conversations (id, role, content, timestamp, memory_ids)
            VALUES (?,?,?,?,?)
        """, (str(uuid.uuid4())[:8], role, content, time.time(), json.dumps(memory_ids or [])))
        await db.commit()

async def get_conversation_history(limit: int = 10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM conversations ORDER BY timestamp DESC LIMIT ?
        """, (limit,))
        rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]
