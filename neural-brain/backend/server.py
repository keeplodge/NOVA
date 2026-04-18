import asyncio
import json
import time
import re
from typing import Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import httpx
from memory import (
    init_db, add_memory, search_memories, get_all_memories,
    get_recent_memories, update_access, save_conversation,
    get_conversation_history, CATEGORIES
)
from reflector import (
    init_pending_table, scheduler_loop, run_reflection,
    list_pending, approve_insight, reject_insight,
)

app = FastAPI(title="NOVA Neural Brain")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

OLLAMA_URL  = "http://localhost:11434"
OLLAMA_MODEL = "llama3"           # change to any model you have pulled
BRAIN_SYSTEM = """You are NOVA Neural Brain — an intelligent persistent memory system for Sir (a Toronto-based trader and entrepreneur).

Your role:
- Answer questions using the provided memory context
- Help store and retrieve information
- Think analytically about trading, business, and personal matters
- Connect patterns across different domains (trading, KeepLodge, Nova Algo)

Personality: Direct, sharp, intelligent. No filler. No "certainly!" or "absolutely!".
Format: Concise. Use bullet points when listing. Max 3 sentences for simple answers.
"""

active_connections: Set[WebSocket] = set()

async def broadcast(event: str, data: dict):
    if not active_connections: return
    msg = json.dumps({"event": event, "data": data})
    dead = set()
    for ws in active_connections:
        try: await ws.send_text(msg)
        except: dead.add(ws)
    active_connections.difference_update(dead)

@app.on_event("startup")
async def startup():
    await init_db()
    await init_pending_table()
    asyncio.create_task(scheduler_loop())
    print("[NOVA Brain] Database initialised, reflector scheduled (10pm EST nightly)")

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    active_connections.add(ws)
    # Send full memory graph on connect
    memories = await get_all_memories()
    await ws.send_text(json.dumps({"event": "brain:init", "data": {"memories": memories, "categories": CATEGORIES}}))
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")
            data  = msg.get("data", {})

            if event == "query":
                await handle_query(ws, data)
            elif event == "memory:add":
                mem = await add_memory(
                    content  = data.get("content", ""),
                    category = data.get("category", "general"),
                    tags     = data.get("tags", []),
                    summary  = data.get("summary", "")
                )
                await broadcast("brain:memory_added", {"memory": mem})
            elif event == "ping":
                await ws.send_text(json.dumps({"event": "pong"}))

    except WebSocketDisconnect:
        active_connections.discard(ws)

async def handle_query(ws: WebSocket, data: dict):
    query = data.get("query", "").strip()
    if not query: return

    await ws.send_text(json.dumps({"event": "brain:think_start", "data": {"query": query}}))

    # Search relevant memories
    relevant = await search_memories(query, limit=6)
    activated_ids = [m["id"] for m in relevant]

    if activated_ids:
        await ws.send_text(json.dumps({"event": "brain:activate", "data": {"ids": activated_ids}}))
        for mid in activated_ids:
            await update_access(mid)

    # Build context
    history = await get_conversation_history(limit=6)
    mem_context = ""
    if relevant:
        mem_context = "\n\nRELEVANT MEMORIES:\n" + "\n".join(
            f"[{m['category'].upper()}] {m['content']}" for m in relevant
        )

    messages = [{"role": "system", "content": BRAIN_SYSTEM + mem_context}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": query})

    await save_conversation("user", query, activated_ids)

    # Stream from Ollama
    full_response = ""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "stream": True,
                "options": {"temperature": 0.7, "num_predict": 512}
            }) as resp:
                if resp.status_code != 200:
                    raise Exception(f"Ollama returned {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line.strip(): continue
                    try:
                        chunk = json.loads(line)
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            full_response += token
                            await ws.send_text(json.dumps({
                                "event": "brain:token",
                                "data": {"token": token}
                            }))
                        if chunk.get("done"):
                            break
                    except: continue

    except Exception as e:
        err = f"[Ollama unavailable — is it running? Error: {e}]"
        full_response = err
        await ws.send_text(json.dumps({"event": "brain:token", "data": {"token": err}}))

    # Auto-save as memory if it contains a fact
    should_save = any(kw in query.lower() for kw in ["remember", "save", "note", "store", "keep"])
    if should_save:
        cat = _classify_category(query + " " + full_response)
        tags = _extract_tags(query)
        mem = await add_memory(content=query, category=cat, tags=tags, summary=full_response[:100])
        await broadcast("brain:memory_added", {"memory": mem})

    await save_conversation("assistant", full_response, activated_ids)
    await ws.send_text(json.dumps({"event": "brain:think_done", "data": {"response": full_response, "memory_ids": activated_ids}}))

def _classify_category(text: str) -> str:
    text = text.lower()
    if any(w in text for w in ["trade", "trading", "nq", "xauusd", "futures", "ict", "signal"]): return "trading"
    if any(w in text for w in ["keeplodge", "villa", "airbnb", "booking", "property"]): return "keeplodge"
    if any(w in text for w in ["nova algo", "nova assistant", "discord", "webhook"]): return "nova"
    if any(w in text for w in ["idea", "build", "launch", "product", "startup"]): return "ideas"
    if any(w in text for w in ["i ", "my ", "me ", "personal", "feel"]): return "personal"
    return "general"

def _extract_tags(text: str) -> list:
    words = re.findall(r'\b[A-Za-z]{4,}\b', text)
    stopwords = {"that", "this", "with", "from", "have", "will", "what", "when", "your", "they"}
    return list({w.lower() for w in words if w.lower() not in stopwords})[:5]

@app.get("/health")
async def health():
    return {"status": "online", "model": OLLAMA_MODEL}

@app.get("/memories")
async def memories():
    return await get_all_memories()

@app.get("/recent")
async def recent():
    return await get_recent_memories(20)

@app.get("/search")
async def search(q: str, limit: int = 6):
    return await search_memories(q, limit=limit)

class MemoryIn(BaseModel):
    content:  str
    category: str = "general"
    summary:  str = ""
    tags:     list = []

@app.post("/memory")
async def add_memory_rest(body: MemoryIn):
    mem = await add_memory(content=body.content, category=body.category,
                           tags=body.tags, summary=body.summary)
    await broadcast("brain:memory_added", {"memory": mem})
    return mem


# ── Insights (reflection output) ─────────────────────────────────────────────

@app.get("/insights/pending")
async def insights_pending():
    return await list_pending()


@app.post("/insights/run")
async def insights_run_now():
    """Manual trigger. Runs the full reflection pass on-demand."""
    result = await run_reflection()
    # Broadcast any auto-approved memories so the sphere updates live
    for ins in result.get("insights", []):
        mem = ins.get("memory")
        if mem:
            await broadcast("brain:memory_added", {"memory": mem})
    await broadcast("brain:insights_updated", {
        "pending_count": len(await list_pending()),
    })
    return result


@app.post("/insights/{iid}/approve")
async def insights_approve(iid: str):
    result = await approve_insight(iid)
    if result.get("ok") and result.get("memory"):
        await broadcast("brain:memory_added", {"memory": result["memory"]})
        await broadcast("brain:insights_updated", {
            "pending_count": len(await list_pending()),
        })
    return result


@app.post("/insights/{iid}/reject")
async def insights_reject(iid: str):
    result = await reject_insight(iid)
    if result.get("ok"):
        await broadcast("brain:insights_updated", {
            "pending_count": len(await list_pending()),
        })
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=7337, reload=False, log_level="warning")
