from __future__ import annotations

import os
import re
import json
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
from html.parser import HTMLParser

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
async def list_wiki_docs(search: Optional[str] = None, user=Depends(get_current_user)):
    """List all wiki documents for the current user, optionally filtered by search query."""
    user_id = user["id"]
    if search:
        import re
        words = [w.strip() for w in re.split(r'\s+', search) if w.strip()]
        if words:
            clauses = []
            for word in words:
                escaped = re.escape(word)
                clauses.append({
                    "$or": [
                        {"title": {"$regex": escaped, "$options": "i"}},
                        {"topic": {"$regex": escaped, "$options": "i"}},
                        {"content": {"$regex": escaped, "$options": "i"}},
                        {"tags": {"$regex": escaped, "$options": "i"}},
                    ]
                })
            match_query = {"user_id": user_id, "$and": clauses}
        else:
            match_query = {"user_id": user_id}
            
        rows = await db.wiki_docs.find(
            match_query,
            {"_id": 0}
        ).sort("updated_at", -1).to_list(1000)
        
        # Build search match snippets
        for row in rows:
            content = row.get("content") or ""
            snippet = ""
            for word in words:
                match = re.search(re.escape(word), content, re.IGNORECASE)
                if match:
                    start = max(0, match.start() - 40)
                    end = min(len(content), match.end() + 80)
                    snippet = ("..." if start > 0 else "") + content[start:end].replace("\n", " ").strip() + ("..." if end < len(content) else "")
                    break
            if not snippet and content:
                snippet = content[:120].replace("\n", " ").strip() + ("..." if len(content) > 120 else "")
            row["snippet"] = snippet
            # Exclude full content from list response
            row.pop("content", None)
        return rows
    else:
        rows = await db.wiki_docs.find(
            {"user_id": user_id},
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

# ==================== Unlinked Mentions Endpoints ====================

class LinkMentionReq(BaseModel):
    source_id: str

def wrap_first_occurrence(content: str, term: str) -> tuple[str, bool]:
    pattern = re.compile(rf"\b({re.escape(term)})\b", re.IGNORECASE)
    match = pattern.search(content)
    if not match:
        return content, False
        
    start, end = match.span()
    left_str = content[:start]
    right_str = content[end:]
    
    if left_str.rstrip().endswith("[[") and right_str.lstrip().startswith("]]"):
        matches = list(pattern.finditer(content))
        for m in matches:
            s, e = m.span()
            l = content[:s]
            r = content[e:]
            if not (l.rstrip().endswith("[[") and r.lstrip().startswith("]]")):
                replacement = f"[[{term}]]"
                new_content = content[:s] + replacement + content[e:]
                return new_content, True
        return content, False
    else:
        replacement = f"[[{term}]]"
        new_content = left_str + replacement + right_str
        return new_content, True

@router.get("/{id_or_title}/unlinked-mentions")
async def get_unlinked_mentions(id_or_title: str, user=Depends(get_current_user)):
    user_id = user["id"]
    target_doc = await db.wiki_docs.find_one({
        "user_id": user_id,
        "$or": [
            {"id": id_or_title},
            {"title": id_or_title}
        ]
    })
    if not target_doc:
        raise HTTPException(status_code=404, detail="Target document not found")
        
    title = target_doc["title"]
    all_docs = await db.wiki_docs.find({
        "user_id": user_id,
        "id": {"$ne": target_doc["id"]}
    }).to_list(1000)
    
    unlinked = []
    pattern = re.compile(rf"\b{re.escape(title)}\b", re.IGNORECASE)
    
    for doc in all_docs:
        links = doc.get("links") or []
        is_linked = any(l.lower() == title.lower() for l in links)
        if not is_linked:
            content = doc.get("content") or ""
            if pattern.search(content):
                unlinked.append({
                    "id": doc["id"],
                    "title": doc["title"],
                    "topic": doc.get("topic", "General")
                })
                
    return unlinked

@router.post("/{id_or_title}/link-mention")
async def link_mention(id_or_title: str, req: LinkMentionReq, user=Depends(get_current_user)):
    user_id = user["id"]
    target_doc = await db.wiki_docs.find_one({
        "user_id": user_id,
        "$or": [
            {"id": id_or_title},
            {"title": id_or_title}
        ]
    })
    if not target_doc:
        raise HTTPException(status_code=404, detail="Target document not found")
        
    term = target_doc["title"]
    source_doc = await db.wiki_docs.find_one({
        "user_id": user_id,
        "id": req.source_id
    })
    if not source_doc:
        raise HTTPException(status_code=404, detail="Source document not found")
        
    new_content, changed = wrap_first_occurrence(source_doc["content"], term)
    if not changed:
        return {"status": "no_change", "message": "No unlinked mentions found in source note content."}
        
    links = parse_markdown_links(new_content)
    
    await db.wiki_docs.update_one(
        {"id": source_doc["id"], "user_id": user_id},
        {"$set": {
            "content": new_content,
            "links": links,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    try:
        save_wiki_to_disk(
            source_doc["title"],
            source_doc["topic"],
            new_content,
            source_doc["tags"],
            source_doc.get("metadata") or {}
        )
    except Exception as exc:
        logger.error("Failed to update synced markdown on disk during link-mention: %s", exc)
        
    await rebuild_all_backlinks(user_id)
    return {"status": "ok", "message": f"Successfully linked '{term}' in note '{source_doc['title']}'."}

@router.get("/audit/rules")
async def audit_trading_rules(user=Depends(get_current_user)):
    user_id = user["id"]
    rules_docs = await db.wiki_docs.find({"user_id": user_id, "topic": "Trading Rules"}).to_list(1000)
    
    documented_max_daily_loss = None
    documented_max_lot = None
    documented_max_net_delta = None
    
    loss_pattern = re.compile(r"max(?:imum)?\s+daily\s+loss\s*(?:limit|\s+)*\s*(?::)?\s*₹?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
    lot_pattern = re.compile(r"max(?:imum)?\s+lot\s*(?:size|\s+)*\s*(?::)?\s*([\d,]+)", re.IGNORECASE)
    delta_pattern = re.compile(r"max(?:imum)?\s+(?:net\s+)?delta\s*(?:exposure|limit|\s+)*\s*(?::)?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
    
    for doc in rules_docs:
        content = doc.get("content") or ""
        for line in content.split("\n"):
            loss_match = loss_pattern.search(line)
            if loss_match and documented_max_daily_loss is None:
                try:
                    documented_max_daily_loss = float(loss_match.group(1).replace(",", ""))
                except ValueError:
                    pass
            
            lot_match = lot_pattern.search(line)
            if lot_match and documented_max_lot is None:
                try:
                    documented_max_lot = int(lot_match.group(1).replace(",", ""))
                except ValueError:
                    pass
                    
            delta_match = delta_pattern.search(line)
            if delta_match and documented_max_net_delta is None:
                try:
                    documented_max_net_delta = float(delta_match.group(1).replace(",", ""))
                except ValueError:
                    pass

    user_doc = await db.users.find_one({"id": user_id})
    settings = dict((user_doc or {}).get("settings") or {})
    active_daily_loss = float(settings.get("max_daily_loss") or user_doc.get("max_daily_loss") or 0.0)
    
    active_max_net_delta = float(settings.get("max_net_delta") or os.environ.get("QUANTG_MAX_NET_DELTA") or 50.0)
    strategies = await db.strategies.find({"user_id": user_id}).to_list(1000)
    
    mismatches = []
    matches = []
    
    if documented_max_daily_loss is not None:
        if active_daily_loss > documented_max_daily_loss:
            mismatches.append({
                "rule": "Max Daily Loss Limit",
                "documented": f"₹{documented_max_daily_loss:,.2f}",
                "active": f"₹{active_daily_loss:,.2f}",
                "severity": "warning",
                "reason": "Active system daily loss limit is looser than documented guidelines."
            })
        else:
            matches.append({
                "rule": "Max Daily Loss Limit",
                "value": f"₹{active_daily_loss:,.2f}"
            })
            
    if documented_max_net_delta is not None:
        if active_max_net_delta > documented_max_net_delta:
            mismatches.append({
                "rule": "Max Net Delta Exposure Limit",
                "documented": str(documented_max_net_delta),
                "active": str(active_max_net_delta),
                "severity": "warning",
                "reason": "Active delta exposure ceiling is looser than documented guidelines."
            })
        else:
            matches.append({
                "rule": "Max Net Delta Exposure Limit",
                "value": str(active_max_net_delta)
            })

    if documented_max_lot is not None:
        for strat in strategies:
            status = str(strat.get("status") or "").lower()
            if status not in ("live", "active", "running"):
                continue
                
            visual = strat.get("visual_config") or {}
            visual_risk = visual.get("risk") or {}
            visual_options = visual.get("options") or {}
            
            configured_lots = int(float(visual_options.get("lots") or 1) or 1)
            max_lot = int(float(visual_risk.get("max_lot") or 0) or 0)
            strat_lot = max_lot if max_lot > 0 else configured_lots
            
            if strat_lot > documented_max_lot:
                mismatches.append({
                    "rule": f"Strategy '{strat.get('name')}' Max Lots",
                    "documented": f"{documented_max_lot} lots",
                    "active": f"{strat_lot} lots",
                    "severity": "warning",
                    "reason": f"Active strategy '{strat.get('name')}' runs with larger trade size than documented rules."
                })
            else:
                matches.append({
                    "rule": f"Strategy '{strat.get('name')}' Max Lots",
                    "value": f"{strat_lot} lots"
                })

    status = "warning" if mismatches else "ok"
    return {
        "status": status,
        "mismatches": mismatches,
        "matches": matches,
        "notes_audited": len(rules_docs)
    }

class WikiSummarizeReq(BaseModel):
    url: str

class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result = []
        self.in_script_or_style = False
        
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "header", "footer", "nav"):
            self.in_script_or_style = True
            
    def handle_endtag(self, tag):
        if tag in ("script", "style", "header", "footer", "nav"):
            self.in_script_or_style = False
            
    def handle_data(self, data):
        if not self.in_script_or_style:
            text = data.strip()
            if text:
                self.result.append(text)
                
    def get_text(self):
        return " ".join(self.result)

@router.post("/summarize")
async def summarize_url(req: WikiSummarizeReq, user=Depends(get_current_user)):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
        
    try:
        import requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        html = res.text
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {exc}")
        
    extractor = HTMLTextExtractor()
    try:
        extractor.feed(html)
        text = extractor.get_text()
    except Exception:
        text = html
        
    trimmed_text = text[:15000].strip()
    if not trimmed_text:
        trimmed_text = "Empty webpage content."

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {
            "title": f"Imported: {url.split('//')[-1][:30]}",
            "topic": "General",
            "tags": ["scraped"],
            "summary_markdown": f"# Scraped Note from URL\n\nReference: {url}\n\n*This content was imported directly because the Gemini API key is not configured in the environment.*\n\n## Scraped Text Preview\n\n{trimmed_text[:1000]}..."
        }
        
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    prompt = f"""You are an expert trading operations and research assistant.
Summarize the key trading rules, setups, decisions, or meeting transcripts from the following text scraped from a webpage, and format it as a clean, highly structured Markdown note.

SCRAPED TEXT:
{trimmed_text}

Output ONLY a JSON block containing the following keys (do not output any other text or markdown wrappers except the JSON):
{{
  "title": "A short, descriptive note title (e.g., Nifty Momentum Setup)",
  "topic": "YouTube transcripts" or "Meeting transcripts" or "Decisions" or "Projects" or "Trading Rules" or "General",
  "tags": ["tag1", "tag2"],
  "summary_markdown": "# Note Title\\n\\nWrite the summarized contents in complete Markdown. Use bullet points and headers."
}}"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "maxOutputTokens": 1500,
        },
    }
    
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        api_res = requests.post(
            gemini_url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=25,
        )
        api_res.raise_for_status()
        res_data = api_res.json()
        
        parts = res_data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text_out = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
        
        parsed_json = json.loads(text_out)
        return parsed_json
    except Exception as exc:
        logger.error("Gemini summarization failed: %s", exc)
        return {
            "title": f"Imported: {url.split('//')[-1][:30]}",
            "topic": "General",
            "tags": ["scraped", "error"],
            "summary_markdown": f"# Scraped Note (AI Summarization Failed)\n\nReference: {url}\n\n*AI Summarization failed: {exc}*\n\n## Scraped Text Preview\n\n{trimmed_text[:1000]}..."
        }
