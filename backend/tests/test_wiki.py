import pytest
import os
import shutil
from pathlib import Path
from routes.wiki import (
    parse_markdown_links,
    parse_markdown_with_frontmatter,
    serialize_markdown_with_frontmatter,
    get_wiki_dir,
    save_wiki_to_disk,
    delete_wiki_from_disk
)

def test_parse_markdown_links():
    # 1. Standard wikilinks
    content = "This is a [[Setup Guide]] and [[Risk Management]]."
    assert parse_markdown_links(content) == ["Risk Management", "Setup Guide"]
    
    # 2. Wikilinks with aliases
    content = "Check out the [[Upstox Setup|Broker Settings]] and [[Trading Rules|Rules]]."
    assert parse_markdown_links(content) == ["Trading Rules", "Upstox Setup"]
    
    # 3. Empty or invalid links
    content = "Invalid [[ ]] and empty [[]]."
    assert parse_markdown_links(content) == []
    
    # 4. Mix of duplicate links
    content = "Read [[Rules]] then [[Rules]] again."
    assert parse_markdown_links(content) == ["Rules"]

def test_parse_markdown_with_frontmatter():
    file_content = """---
topic: YouTube transcripts
tags: [trading, guide, live]
url: http://example.com/video
date: 2026-06-19
---
# QuantG Overview
This is the body of the note.
See Also: [[Upstox Setup]].
"""
    fm_data, body, title = parse_markdown_with_frontmatter(file_content, "Fallback")
    
    assert title == "QuantG Overview"
    assert fm_data["topic"] == "YouTube transcripts"
    assert fm_data["tags"] == ["trading", "guide", "live"]
    assert fm_data["metadata"]["url"] == "http://example.com/video"
    assert fm_data["metadata"]["date"] == "2026-06-19"
    assert "This is the body of the note." in body

def test_serialize_markdown_with_frontmatter():
    title = "QuantG Overview"
    topic = "YouTube transcripts"
    content = "This is the body of the note.\nSee Also: [[Upstox Setup]]."
    tags = ["trading", "guide"]
    metadata = {"url": "http://example.com/video", "date": "2026-06-19"}
    
    serialized = serialize_markdown_with_frontmatter(title, topic, content, tags, metadata)
    
    assert serialized.startswith("---")
    assert "title: QuantG Overview" in serialized
    assert "topic: YouTube transcripts" in serialized
    assert "tags: [trading, guide]" in serialized
    assert "url: http://example.com/video" in serialized
    assert "date: 2026-06-19" in serialized
    assert "# QuantG Overview" in serialized
    assert "This is the body of the note." in serialized

def test_disk_operations():
    import tempfile
    try:
        temp_dir_obj = tempfile.TemporaryDirectory()
        temp_dir = temp_dir_obj.name
    except Exception:
        # Fallback to local scratch folder if system Temp lacks permissions
        scratch_dir = Path(__file__).resolve().parent.parent / "scratch" / "test_wiki_tmp"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = str(scratch_dir)
        temp_dir_obj = None

    # Mock WIKI_DIR env var to use our custom temp_dir
    os.environ["WIKI_DIR"] = temp_dir
    try:
        assert str(get_wiki_dir()) == temp_dir
        
        title = "Test Page Title"
        topic = "Trading Rules"
        content = "Rules: [[Rule 1]]."
        tags = ["rules"]
        metadata = {"date": "2026-06-19"}
        
        # Save to disk
        filepath = save_wiki_to_disk(title, topic, content, tags, metadata)
        assert filepath.exists()
        assert filepath.name == "Test Page Title.md"
        
        # Read back and check
        read_content = filepath.read_text(encoding="utf-8")
        assert "title: Test Page Title" in read_content
        
        # Delete from disk
        delete_wiki_from_disk(title, topic)
        assert not filepath.exists()
    finally:
        # Clean up env var
        os.environ.pop("WIKI_DIR", None)
        if temp_dir_obj:
            try:
                temp_dir_obj.cleanup()
            except Exception:
                pass
        else:
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
