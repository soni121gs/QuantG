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

def test_wrap_first_occurrence():
    from routes.wiki import wrap_first_occurrence
    
    # 1. Simple replacement
    content = "This is a Upstox setup guide."
    new_content, changed = wrap_first_occurrence(content, "Upstox setup")
    assert changed is True
    assert new_content == "This is a [[Upstox setup]] guide."
    
    # 2. Case insensitive match, but wraps with term casing
    content = "Check upstox setup details."
    new_content, changed = wrap_first_occurrence(content, "Upstox Setup")
    assert changed is True
    assert new_content == "Check [[Upstox Setup]] details."
    
    # 3. Already linked, should not wrap again
    content = "This is already [[Upstox setup]] linked."
    new_content, changed = wrap_first_occurrence(content, "Upstox setup")
    assert changed is False
    assert new_content == content
    
    # 4. Multiple occurrences: skip the linked one and wrap the first unlinked one
    content = "First [[Upstox setup]] is linked, but second Upstox setup is not."
    new_content, changed = wrap_first_occurrence(content, "Upstox setup")
    assert changed is True
    assert new_content == "First [[Upstox setup]] is linked, but second [[Upstox setup]] is not."

def test_html_text_extractor():
    from routes.wiki import HTMLTextExtractor
    
    html = """
    <html>
        <head><title>Test Page</title></head>
        <style>body { color: red; }</style>
        <body>
            <header>Header content</header>
            <nav>Nav links</nav>
            <h1>Setup Guide</h1>
            <p>Use Upstox keys to authenticate.</p>
            <script>console.log("hello");</script>
            <footer>Footer notes</footer>
        </body>
    </html>
    """
    extractor = HTMLTextExtractor()
    extractor.feed(html)
    text = extractor.get_text()
    
    # Scripts, styles, header, nav, footer should be stripped
    assert "Setup Guide" in text
    assert "Use Upstox keys" in text
    assert "console.log" not in text
    assert "color: red" not in text
    assert "Header content" not in text
    assert "Footer notes" not in text

def test_rules_extraction_logic():
    import re
    loss_pattern = re.compile(r"max(?:imum)?\s+daily\s+loss\s*(?:limit|\s+)*\s*(?::)?\s*₹?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
    lot_pattern = re.compile(r"max(?:imum)?\s+lot\s*(?:size|\s+)*\s*(?::)?\s*([\d,]+)", re.IGNORECASE)
    delta_pattern = re.compile(r"max(?:imum)?\s+(?:net\s+)?delta\s*(?:exposure|limit|\s+)*\s*(?::)?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
    
    content = """
    # Trading Rules
    - Max daily loss limit: ₹5,000
    - Max Lot Size: 2 lots
    - Max net delta exposure limit: 50.5
    """
    
    daily_loss = None
    max_lot = None
    net_delta = None
    
    for line in content.split("\n"):
        loss_match = loss_pattern.search(line)
        if loss_match:
            daily_loss = float(loss_match.group(1).replace(",", ""))
        lot_match = lot_pattern.search(line)
        if lot_match:
            max_lot = int(lot_match.group(1))
        delta_match = delta_pattern.search(line)
        if delta_match:
            net_delta = float(delta_match.group(1))
            
    assert daily_loss == 5000.0
    assert max_lot == 2
    assert net_delta == 50.5
