"""
NOVA Neural Brain — Vault & Conversation Ingestor

Scans multiple sources and imports .md files into brain.db:
  1. Obsidian vault    — C:/Users/User/nova/nova-brain  (legacy notes)
  2. Conversation logs — C:/Users/User/nova/neural-brain/conversations  (live sessions)

Run: python import_obsidian.py
     python import_obsidian.py --conversations-only
     python import_obsidian.py --vault-only
"""
import asyncio
import argparse
import re
import uuid
import time
import json
import math
import random
from pathlib import Path
import aiosqlite

DB_PATH = Path(__file__).parent / "brain.db"

# ── Ingestion sources ─────────────────────────────────────────────────────────
# (path, default_category, label)
SOURCES = [
    (Path("C:/Users/User/nova/nova-brain"),                      None,   "vault"),
    (Path("C:/Users/User/nova/neural-brain/conversations"),      "nova", "conversations"),
]

# ── Folder → category map (applied to file paths) ─────────────────────────────
FOLDER_CATS = {
    "01_Trade_Logs":  "trading",
    "06_KeepLodge":   "keeplodge",
    "07_Sessions":    "nova",
    "conversations":  "nova",
}

# ── Exact filename → category map ─────────────────────────────────────────────
FILE_CATS = {
    "NOVA-ICT-Strategy.md": "trading",
    "NOVA-PRD.md":           "nova",
}

# ── Filename keyword → category (lowercase substring match) ───────────────────
FILENAME_HINTS = [
    ("probuild",   "probuild"),
    ("trade",      "trading"),
    ("tradelog",   "trading"),
    ("backtest",   "trading"),
    ("ict",        "trading"),
    ("session",    "nova"),
    ("brief",      "nova"),
    ("debrief",    "nova"),
    ("keeplodge",  "keeplodge"),
    ("booking",    "keeplodge"),
    ("client",     "keeplodge"),
    ("idea",       "ideas"),
    ("personal",   "personal"),
]


def classify(path: Path, source_default: str | None) -> str:
    """Determine category for a file.
    Order: exact filename → filename hint → folder → source default → general.
    Filename wins over folder so tag-prefixed conversations (probuild-*, etc.)
    land in the right category even when they live in a general folder.
    """
    p = str(path).replace("\\", "/").lower()

    if path.name in FILE_CATS:
        return FILE_CATS[path.name]

    name_lower = path.stem.lower()
    for kw, cat in FILENAME_HINTS:
        if kw in name_lower:
            return cat

    for folder, cat in FOLDER_CATS.items():
        if f"/{folder.lower()}/" in p or p.endswith(f"/{folder.lower()}"):
            return cat

    return source_default or "general"


def extract_summary(text: str, max_len: int = 120) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if len(line) > 20 and not line.startswith("|") and not line.startswith("-"):
            return line[:max_len]
    return text[:max_len]


def extract_tags(text: str, cat: str) -> list:
    words = re.findall(r'\b[A-Za-z]{4,}\b', text)
    stopwords = {
        "that","this","with","from","have","will","what","when","your",
        "they","been","were","would","could","should","which","their",
        "about","into","after","where","these","those","more","also",
        "back","some","then","than","just","like","only","over","such",
        "them","here","even","does","done","need","want","make","used",
    }
    tags = list({w.lower() for w in words if w.lower() not in stopwords})[:6]
    tags.append(cat)
    return tags[:6]


def sphere_pos():
    theta = random.uniform(0, 2 * math.pi)
    phi   = math.acos(random.uniform(-1, 1))
    r     = 2.5 + random.uniform(-0.4, 0.4)
    return (
        r * math.sin(phi) * math.cos(theta),
        r * math.sin(phi) * math.sin(theta),
        r * math.cos(phi),
    )


def condense_trade_log(text: str, fpath: Path) -> str:
    """Reduce a trade log to the vital stats row-by-row."""
    lines = []
    for key in ["Date", "Session", "Side", "Entry Price", "Stop Loss",
                "Take Profit", "Risk", "Reward", "R:R", "Result", "Notes"]:
        m = re.search(rf'\|\s*{key}\s*\|\s*(.+?)\s*\|', text)
        if m and m.group(1).strip() not in ("", "—", "TBD", "Value"):
            lines.append(f"{key}: {m.group(1).strip()}")
    if lines:
        return f"Trade: {fpath.stem}\n" + "\n".join(lines)
    return text[:400]


async def ingest_source(db, src_path: Path, default_cat: str | None, label: str,
                        existing: set) -> tuple[int, int]:
    if not src_path.exists():
        print(f"  [{label:14}] SKIP — path does not exist: {src_path}")
        return 0, 0

    files = list(src_path.rglob("*.md"))
    print(f"\n[{label}] Scanning {src_path} — {len(files)} markdown files")

    imported = 0
    skipped  = 0

    for fpath in files:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as e:
            print(f"  SKIP (read error): {fpath.name} — {e}")
            skipped += 1
            continue

        if len(text) < 30:
            skipped += 1
            continue

        cat = classify(fpath, default_cat)

        if cat == "trading" and "Trade Log" in text:
            content = condense_trade_log(text, fpath)
        else:
            content = text[:600]

        if content[:60] in existing:
            skipped += 1
            continue

        mid     = str(uuid.uuid4())[:8]
        summary = extract_summary(text)
        tags    = extract_tags(text, cat)
        x, y, z = sphere_pos()
        now     = time.time()

        await db.execute("""
            INSERT INTO memories (id, content, summary, category, tags, created_at, updated_at, x, y, z)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (mid, content, summary, cat, json.dumps(tags), now, now, x, y, z))

        existing.add(content[:60])
        imported += 1
        print(f"  [{cat:10}] {fpath.name[:60]}")

    return imported, skipped


async def run(conversations_only: bool = False, vault_only: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT content FROM memories")
        existing = {r[0][:60] for r in await cur.fetchall()}

        total_imp = 0
        total_skip = 0

        for src_path, default_cat, label in SOURCES:
            if conversations_only and label != "conversations":
                continue
            if vault_only and label != "vault":
                continue

            imp, skip = await ingest_source(db, src_path, default_cat, label, existing)
            total_imp  += imp
            total_skip += skip

        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM memories")
        total = (await cur.fetchone())[0]

    print(f"\nDONE — Imported {total_imp} | Skipped {total_skip} | Total memories in brain.db: {total}")


def main():
    parser = argparse.ArgumentParser(description="Ingest markdown into NOVA Neural Brain")
    parser.add_argument("--conversations-only", action="store_true",
                        help="Only scan the neural-brain/conversations folder")
    parser.add_argument("--vault-only", action="store_true",
                        help="Only scan the nova-brain Obsidian vault")
    args = parser.parse_args()

    asyncio.run(run(
        conversations_only=args.conversations_only,
        vault_only=args.vault_only,
    ))


if __name__ == "__main__":
    main()
