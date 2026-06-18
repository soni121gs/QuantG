from __future__ import annotations

import os
import re
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import db, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wiki", tags=["Wiki & Notes"])

# ==================== Pydantic Models ====================
class WikiDocReq(BaseModel):
    title: str
    topic: str
    content: str
    tags: List[str] = []
    metadata: Dict[str, Any] = {}

class WikiDocOut(BaseModel):
    id: str
    title: str
    topic: str
    content: str
    tags: List[str]
    links: List[str]
    backlinks: List[str]
    metadata: Dict[str, Any]
    created_at: str
    updated_at: str

# ==================== Helper Utilities ====================
def get_wiki_dir() -> Path:
    """Resolve the wiki directory path dynamically."""
    env_val = os.environ.get("WIKI_DIR")
    if env_val:
        return Path(env_val)
    
    # Check if running inside container or standard layout
    container_path = Path("/app/wiki")
    if container_path.exists():
        return container_path
        
    # Default: root of the project (2 levels up from backend/routes)
    root = Path(__file__).resolve().parent.parent.parent
    return root / "wiki"

def parse_markdown_links(content: str) -> List[str]:
    """Extract all outgoing double-bracket wikilinks from markdown content."""
    # Matches [[Link Target]] or [[Link Target|Alias Text]]
    pattern = r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]"
    found = re.findall(pattern, content)
    # Strip whitespace, filter empty, and return unique items
    return sorted(list(set(t.strip() for t in found if t.strip())))

def parse_markdown_with_frontmatter(file_content: str, fallback_title: str) -> tuple[dict, str, str]:
    """Parse YAML-like frontmatter and body from a markdown file."""
    metadata = {}
    topic = "General"
    content_body = file_content
    title = fallback_title

    # Normalise line endings
    file_content = file_content.replace("\r\n", "\n")

    if file_content.startswith("---"):
        parts = file_content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            content_body = parts[2].strip()
            
            for line in frontmatter.strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip().lower()
                    v = v.strip()
                    if k == "tags":
                        # Parse tags list: [tag1, tag2]
                        v_clean = v.strip("[]").strip()
                        if v_clean:
                            metadata["tags"] = [t.strip() for t in v_clean.split(",") if t.strip()]
                        else:
                            metadata["tags"] = []
                    elif k in {"topic", "title", "url", "date"}:
                        metadata[k] = v
                    else:
                        metadata[k] = v

    # Extract title from metadata or first H1 header
    if "title" in metadata:
        title = metadata["title"]
    else:
        # Check first line H1 `# Title`
        first_line_match = re.match(r"^#\s+(.+)$", content_body.split("\n")[0])
        if first_line_match:
            title = first_line_match.group(1).strip()

    # Extract topic from metadata
    if "topic" in metadata:
        topic = metadata["topic"]

    # Ensure metadata has core keys
    tags = metadata.get("tags") or []
    other_metadata = {k: v for k, v in metadata.items() if k not in {"title", "topic", "tags"}}

    return {"title": title, "topic": topic, "tags": tags, "metadata": other_metadata}, content_body, title

def serialize_markdown_with_frontmatter(title: str, topic: str, content: str, tags: list, metadata: dict) -> str:
    """Create a markdown string with Obsidian-compatible YAML frontmatter."""
    lines = ["---"]
    lines.append(f"title: {title}")
    lines.append(f"topic: {topic}")
    tags_str = ", ".join(tags)
    lines.append(f"tags: [{tags_str}]")
    
    for k, v in metadata.items():
        if k not in {"title", "topic", "tags"}:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    
    # Standardise title header in content body
    has_h1 = content.strip().startswith("# ")
    if not has_h1:
        lines.append(f"# {title}")
        lines.append("")
        
    lines.append(content.strip())
    return "\n".join(lines)

def save_wiki_to_disk(title: str, topic: str, content: str, tags: list, metadata: dict):
    """Write markdown file to disk in topic directory."""
    wiki_dir = get_wiki_dir()
    topic_dir = wiki_dir / topic
    topic_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize title for filename
    filename = "".join(c for c in title if c not in r'\/:*?"<>|') + ".md"
    filepath = topic_dir / filename
    
    file_content = serialize_markdown_with_frontmatter(title, topic, content, tags, metadata)
    filepath.write_text(file_content, encoding="utf-8")
    return filepath

def delete_wiki_from_disk(title: str, topic: str):
    """Delete markdown file from disk."""
    wiki_dir = get_wiki_dir()
    filename = "".join(c for c in title if c not in r'\/:*?"<>|') + ".md"
    filepath = wiki_dir / topic / filename
    if filepath.exists():
        filepath.unlink()
        
    # Clean up empty topic directories (except the default/required ones)
    topic_dir = wiki_dir / topic
    if topic_dir.exists() and not any(topic_dir.iterdir()):
        try:
            topic_dir.rmdir()
        except Exception:
            pass

async def rebuild_all_backlinks(user_id: str):
    """Scan all wiki documents for user and rebuild backlinks array."""
    docs = await db.wiki_docs.find({"user_id": user_id}).to_list(1000)
    title_to_backlinks = {doc["title"]: set() for doc in docs}
    
    # Populate backlinks mapping
    for doc in docs:
        links = doc.get("links") or []
        for target_title in links:
            if target_title in title_to_backlinks:
                title_to_backlinks[target_title].add(doc["title"])
                
    # Update documents
    for doc in docs:
        bl = sorted(list(title_to_backlinks[doc["title"]]))
        await db.wiki_docs.update_one(
            {"id": doc["id"], "user_id": user_id},
            {"$set": {"backlinks": bl}}
        )

# ==================== API Endpoints ====================

@router.get("")
async def list_wiki_docs(user=Depends(get_current_user)):
    """List all wiki documents for the current user."""
    rows = await db.wiki_docs.find(
        {"user_id": user["id"]},
        {"_id": 0, "content": 0}  # Exclude body to keep list payloads lightweight
    ).sort("updated_at", -1).to_list(1000)
    return rows

@router.get("/{id_or_title}")
async def get_wiki_doc(id_or_title: str, user=Depends(get_current_user)):
    """Retrieve a single wiki document by id/slug or exact title."""
    doc = await db.wiki_docs.find_one({
        "user_id": user["id"],
        "$or": [
            {"id": id_or_title},
            {"title": id_or_title}
        ]
    }, {"_id": 0})
    
    if not doc:
        raise HTTPException(status_code=404, detail="Wiki document not found")
    return doc

@router.post("")
async def create_wiki_doc(req: WikiDocReq, user=Depends(get_current_user)):
    """Create a new wiki note."""
    user_id = user["id"]
    slug = re.sub(r'[^a-z0-9]+', '-', req.title.lower()).strip('-')
    if not slug:
        slug = str(uuid.uuid4())[:8]
        
    # Check duplicate title
    exists = await db.wiki_docs.find_one({"user_id": user_id, "title": req.title})
    if exists:
        raise HTTPException(status_code=400, detail="A document with this title already exists")

    now_str = datetime.now(timezone.utc).isoformat()
    links = parse_markdown_links(req.content)
    
    doc = {
        "id": slug,
        "title": req.title,
        "topic": req.topic,
        "content": req.content,
        "tags": req.tags,
        "links": links,
        "backlinks": [],
        "metadata": req.metadata,
        "user_id": user_id,
        "created_at": now_str,
        "updated_at": now_str,
    }
    
    # Save to MongoDB
    await db.wiki_docs.insert_one(doc)
    doc.pop("_id", None)
    
    # Write to local disk (Obsidian sync)
    try:
        save_wiki_to_disk(req.title, req.topic, req.content, req.tags, req.metadata)
    except Exception as exc:
        logger.error("Failed to write wiki note to disk: %s", exc)

    # Rebuild graph links
    await rebuild_all_backlinks(user_id)
    
    # Refetch updated backlinks
    updated = await db.wiki_docs.find_one({"id": slug, "user_id": user_id}, {"_id": 0})
    return updated

@router.put("/{id}")
async def update_wiki_doc(id: str, req: WikiDocReq, user=Depends(get_current_user)):
    """Update an existing wiki note."""
    user_id = user["id"]
    old_doc = await db.wiki_docs.find_one({"id": id, "user_id": user_id})
    if not old_doc:
        raise HTTPException(status_code=404, detail="Wiki document not found")
        
    now_str = datetime.now(timezone.utc).isoformat()
    links = parse_markdown_links(req.content)
    
    # Check duplicate title if title changed
    if req.title != old_doc["title"]:
        exists = await db.wiki_docs.find_one({"user_id": user_id, "title": req.title})
        if exists:
            raise HTTPException(status_code=400, detail="A document with this title already exists")

    # Update MongoDB
    await db.wiki_docs.update_one(
        {"id": id, "user_id": user_id},
        {"$set": {
            "title": req.title,
            "topic": req.topic,
            "content": req.content,
            "tags": req.tags,
            "links": links,
            "metadata": req.metadata,
            "updated_at": now_str
        }}
    )
    
    # Update files on disk
    try:
        # Delete old file if renamed or moved to different topic folder
        if req.title != old_doc["title"] or req.topic != old_doc["topic"]:
            delete_wiki_from_disk(old_doc["title"], old_doc["topic"])
        save_wiki_to_disk(req.title, req.topic, req.content, req.tags, req.metadata)
    except Exception as exc:
        logger.error("Failed to update wiki note on disk: %s", exc)
        
    # Rebuild graph links
    await rebuild_all_backlinks(user_id)
    
    # Refetch document
    updated = await db.wiki_docs.find_one({"id": id, "user_id": user_id}, {"_id": 0})
    return updated

@router.delete("/{id}")
async def delete_wiki_doc(id: str, user=Depends(get_current_user)):
    """Delete a wiki note."""
    user_id = user["id"]
    doc = await db.wiki_docs.find_one({"id": id, "user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Wiki document not found")
        
    # Remove from MongoDB
    await db.wiki_docs.delete_one({"id": id, "user_id": user_id})
    
    # Remove from disk
    try:
        delete_wiki_from_disk(doc["title"], doc["topic"])
    except Exception as exc:
        logger.error("Failed to delete wiki note from disk: %s", exc)
        
    # Rebuild graph links
    await rebuild_all_backlinks(user_id)
    return {"status": "deleted", "id": id}

@router.post("/sync")
async def sync_wiki_directory(user=Depends(get_current_user)):
    """Scan the local wiki folder and update the MongoDB database (Obsidian -> App Sync)."""
    user_id = user["id"]
    wiki_dir = get_wiki_dir()
    
    if not wiki_dir.exists():
        wiki_dir.mkdir(parents=True, exist_ok=True)
        return {"status": "ok", "imported": 0, "deleted": 0, "message": "Wiki directory was empty and was created."}

    # 1. Scan and import files from disk
    all_md_files = list(wiki_dir.glob("**/*.md"))
    processed_titles = set()
    imported_count = 0
    now_str = datetime.now(timezone.utc).isoformat()
    
    for filepath in all_md_files:
        try:
            # Topic directory is the subdirectory directly under wiki/
            try:
                rel_path = filepath.relative_to(wiki_dir)
                if len(rel_path.parts) > 1:
                    topic = rel_path.parts[0]
                else:
                    topic = "General"
            except Exception:
                topic = "General"
                
            file_content = filepath.read_text(encoding="utf-8")
            fallback_title = filepath.stem
            
            fm_data, body, title = parse_markdown_with_frontmatter(file_content, fallback_title)
            processed_titles.add(title)
            
            # Extract links
            links = parse_markdown_links(body)
            slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
            if not slug:
                slug = str(uuid.uuid4())[:8]

            # Upsert document in MongoDB
            exists = await db.wiki_docs.find_one({"user_id": user_id, "title": title})
            if exists:
                # Update existing
                await db.wiki_docs.update_one(
                    {"id": exists["id"], "user_id": user_id},
                    {"$set": {
                        "topic": topic,
                        "content": body,
                        "tags": fm_data["tags"],
                        "links": links,
                        "metadata": fm_data["metadata"],
                        "updated_at": now_str
                    }}
                )
            else:
                # Insert new
                doc = {
                    "id": slug,
                    "title": title,
                    "topic": topic,
                    "content": body,
                    "tags": fm_data["tags"],
                    "links": links,
                    "backlinks": [],
                    "metadata": fm_data["metadata"],
                    "user_id": user_id,
                    "created_at": now_str,
                    "updated_at": now_str,
                }
                await db.wiki_docs.insert_one(doc)
                
            imported_count += 1
        except Exception as exc:
            logger.error("Failed to sync file %s: %s", filepath, exc)

    # 2. Delete database items that are no longer present on disk
    db_docs = await db.wiki_docs.find({"user_id": user_id}).to_list(1000)
    deleted_count = 0
    
    for doc in db_docs:
        if doc["title"] not in processed_titles:
            await db.wiki_docs.delete_one({"id": doc["id"], "user_id": user_id})
            deleted_count += 1

    # 3. Rebuild backlinks for correctness
    await rebuild_all_backlinks(user_id)
    
    return {
        "status": "ok",
        "imported": imported_count,
        "deleted": deleted_count,
        "message": f"Successfully imported {imported_count} files and pruned {deleted_count} stale entries."
    }

@router.get("/graph/data")
async def get_wiki_graph_data(user=Depends(get_current_user)):
    """Generate link-graph mapping for the visualization layer."""
    user_id = user["id"]
    docs = await db.wiki_docs.find({"user_id": user_id}, {"_id": 0, "title": 1, "topic": 1, "links": 1}).to_list(1000)
    
    nodes = []
    links = []
    titles_in_db = {d["title"] for d in docs}
    
    for doc in docs:
        nodes.append({
            "id": doc["title"],
            "topic": doc.get("topic", "General")
        })
        
        # Build edges (links to other notes in the db)
        outgoing = doc.get("links") or []
        for target in outgoing:
            if target in titles_in_db:
                links.append({
                    "source": doc["title"],
                    "target": target
                })
                
    return {
        "nodes": nodes,
        "links": links
    }
