import React, { useState, useEffect, useRef } from "react";
import { api } from "../lib/api";
import { toast } from "sonner";
import {
  FileText,
  FolderOpen,
  Search,
  Plus,
  RefreshCw,
  Edit2,
  Trash2,
  BookOpen,
  ExternalLink,
  Tag,
  Calendar,
  Share2,
  ChevronRight,
  ChevronDown,
} from "lucide-react";
import { Button } from "../components/ui/button";

// ==================== Simple Markdown Parser & Renderer ====================
function renderMarkdownContent(content, onSelectNote) {
  if (!content) return null;

  // 1. Helper to render inline elements (bold, code, wikilinks)
  const parseInline = (text) => {
    // Regex matches [[Wiki Link]] or [[Wiki Link|Display Name]]
    const parts = text.split(/(\[\[[^\]]+\]\])/g);
    return parts.map((part, i) => {
      const match = part.match(/^\[\[([^\]|]+)(?:\|([^\]]+))?\]\]$/);
      if (match) {
        const target = match[1].trim();
        const display = match[2] ? match[2].trim() : target;
        return (
          <button
            key={i}
            type="button"
            onClick={() => onSelectNote(target)}
            className="text-[var(--qd-accent)] hover:underline font-bold inline-block bg-transparent p-0 border-none cursor-pointer align-baseline"
          >
            {display}
          </button>
        );
      }

      // Handle bold text **bold**
      const boldParts = part.split(/(\*\*[^*]+\*\*)/g);
      return boldParts.map((bPart, j) => {
        const bMatch = bPart.match(/^\*\*([^*]+)\*\*$/);
        if (bMatch) {
          return <strong key={j} className="font-bold text-[var(--qd-text)]">{bMatch[1]}</strong>;
        }

        // Handle inline code `code`
        const codeParts = bPart.split(/(`[^`]+`)/g);
        return codeParts.map((cPart, k) => {
          const cMatch = cPart.match(/^`([^`]+)`$/);
          if (cMatch) {
            return (
              <code key={k} className="px-1.5 py-0.5 rounded bg-[var(--qd-surface-3)] font-mono text-xs text-[var(--qd-warn)]">
                {cMatch[1]}
              </code>
            );
          }
          return cPart;
        });
      });
    });
  };

  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const elements = [];
  let codeBlock = [];
  let isCode = false;
  let listItems = [];

  const flushList = (key) => {
    if (listItems.length > 0) {
      elements.push(
        <ul key={key} className="list-disc pl-5 my-3 space-y-1.5 text-[var(--qd-text-2)] text-sm">
          {listItems}
        </ul>
      );
      listItems = [];
    }
  };

  lines.forEach((line, index) => {
    // Fenced code block: ```python
    if (line.trim().startsWith("```")) {
      if (isCode) {
        // End of code block
        elements.push(
          <pre key={`code-${index}`} className="p-4 my-3 rounded bg-[var(--qd-surface-2)] border border-[var(--qd-border)] overflow-x-auto font-mono text-xs text-[var(--qd-text-2)]">
            <code>{codeBlock.join("\n")}</code>
          </pre>
        );
        codeBlock = [];
        isCode = false;
      } else {
        // Start of code block
        flushList(`list-before-code-${index}`);
        isCode = true;
      }
      return;
    }

    if (isCode) {
      codeBlock.push(line);
      return;
    }

    // Headers
    if (line.startsWith("# ")) {
      flushList(`list-before-h1-${index}`);
      elements.push(
        <h1 key={index} className="text-2xl font-extrabold font-head text-[var(--qd-text)] mt-6 mb-3 border-b border-[var(--qd-border)] pb-2">
          {parseInline(line.substring(2))}
        </h1>
      );
    } else if (line.startsWith("## ")) {
      flushList(`list-before-h2-${index}`);
      elements.push(
        <h2 key={index} className="text-xl font-bold font-head text-[var(--qd-text)] mt-5 mb-2.5">
          {parseInline(line.substring(3))}
        </h2>
      );
    } else if (line.startsWith("### ")) {
      flushList(`list-before-h3-${index}`);
      elements.push(
        <h3 key={index} className="text-lg font-semibold font-head text-[var(--qd-text)] mt-4 mb-2">
          {parseInline(line.substring(4))}
        </h3>
      );
    }
    // Bullet lists
    else if (line.trim().startsWith("- ") || line.trim().startsWith("* ")) {
      const bulletContent = line.trim().substring(2);
      listItems.push(<li key={`li-${index}`}>{parseInline(bulletContent)}</li>);
    }
    // Horizontal rule
    else if (line.trim() === "---") {
      flushList(`list-before-hr-${index}`);
      elements.push(<hr key={index} className="my-6 border-[var(--qd-border)]" />);
    }
    // Empty line
    else if (line.trim() === "") {
      flushList(`list-before-empty-${index}`);
      elements.push(<div key={index} className="h-2" />);
    }
    // Standard paragraph
    else {
      flushList(`list-before-p-${index}`);
      elements.push(
        <p key={index} className="leading-relaxed text-sm text-[var(--qd-text-2)] my-2">
          {parseInline(line)}
        </p>
      );
    }
  });

  flushList("list-final");
  return <div className="space-y-1">{elements}</div>;
}

// ==================== Interactive Spring Physics Graph Component ====================
function WikiGraph({ data, currentTitle, onSelectNode }) {
  const canvasRef = useRef(null);
  const [nodes, setNodes] = useState([]);
  const [links, setLinks] = useState([]);
  const [hoveredNode, setHoveredNode] = useState(null);
  const dragNodeRef = useRef(null);

  // Dimensions
  const width = 350;
  const height = 300;
  const center = { x: width / 2, y: height / 2 };

  // Set up nodes and links when data changes
  useEffect(() => {
    if (!data || !data.nodes) return;

    // Keep existing node positions if they match to prevent layout snapping on update
    const existingNodesMap = new Map(nodes.map((n) => [n.id, n]));
    
    const newNodes = data.nodes.map((node) => {
      const existing = existingNodesMap.get(node.id);
      return {
        id: node.id,
        topic: node.topic,
        x: existing ? existing.x : center.x + (Math.random() - 0.5) * 80,
        y: existing ? existing.y : center.y + (Math.random() - 0.5) * 80,
        vx: existing ? existing.vx : 0,
        vy: existing ? existing.vy : 0,
        r: node.id === currentTitle ? 8 : 5,
      };
    });

    const newLinks = data.links.map((link) => ({
      source: link.source,
      target: link.target,
    }));

    setNodes(newNodes);
    setLinks(newLinks);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, currentTitle]);

  // Spring physics loop
  useEffect(() => {
    if (nodes.length === 0) return;

    const REPULSION = 1000;
    const ATTRACTION = 0.04;
    const GRAVITY = 0.02;
    const DAMPING = 0.85;

    let animId;

    const step = () => {
      setNodes((prevNodes) => {
        const nextNodes = prevNodes.map((n) => ({ ...n }));
        const nodeMap = new Map(nextNodes.map((n) => [n.id, n]));

        // 1. Coulomb repulsion (nodes push away from each other)
        for (let i = 0; i < nextNodes.length; i++) {
          const n1 = nextNodes[i];
          if (n1 === dragNodeRef.current) continue;

          for (let j = i + 1; j < nextNodes.length; j++) {
            const n2 = nextNodes[j];
            const dx = n2.x - n1.x || 0.1;
            const dy = n2.y - n1.y || 0.1;
            const distSq = dx * dx + dy * dy;
            const dist = Math.sqrt(distSq) || 1;

            if (dist < 180) {
              const force = REPULSION / (distSq + 20);
              const fx = (dx / dist) * force;
              const fy = (dy / dist) * force;

              n1.vx -= fx;
              n1.vy -= fy;
              n2.vx += fx;
              n2.vy += fy;
            }
          }
        }

        // 2. Hooke's attraction (linked nodes pull together)
        links.forEach((link) => {
          const sNode = nodeMap.get(link.source);
          const tNode = nodeMap.get(link.target);
          if (!sNode || !tNode) return;

          const dx = tNode.x - sNode.x;
          const dy = tNode.y - sNode.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const targetDist = 70; // natural link distance
          const force = (dist - targetDist) * ATTRACTION;

          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;

          if (sNode !== dragNodeRef.current) {
            sNode.vx += fx;
            sNode.vy += fy;
          }
          if (tNode !== dragNodeRef.current) {
            tNode.vx -= fx;
            tNode.vy -= fy;
          }
        });

        // 3. Gravity pulling toward center
        nextNodes.forEach((n) => {
          if (n === dragNodeRef.current) return;
          n.vx += (center.x - n.x) * GRAVITY;
          n.vy += (center.y - n.y) * GRAVITY;

          // Apply velocities & damping
          n.vx *= DAMPING;
          n.vy *= DAMPING;
          n.x += n.vx;
          n.y += n.vy;

          // Contain nodes within boundaries
          n.x = Math.max(10, Math.min(width - 10, n.x));
          n.y = Math.max(10, Math.min(height - 10, n.y));
        });

        return nextNodes;
      });

      animId = requestAnimationFrame(step);
    };

    animId = requestAnimationFrame(step);
    return () => cancelAnimationFrame(animId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes.length, links]);

  // Drag and drop handlers
  const handleMouseDown = (node, e) => {
    dragNodeRef.current = node;
    e.preventDefault();
  };

  const handleMouseMove = (e) => {
    if (!dragNodeRef.current || !canvasRef.current) return;
    const rect = canvasRef.current.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;

    setNodes((prevNodes) =>
      prevNodes.map((n) => {
        if (n.id === dragNodeRef.current.id) {
          n.x = mouseX;
          n.y = mouseY;
          n.vx = 0;
          n.vy = 0;
          dragNodeRef.current = n;
        }
        return n;
      })
    );
  };

  const handleMouseUp = () => {
    dragNodeRef.current = null;
  };

  const getTopicColor = (topic) => {
    const map = {
      "YouTube transcripts": "var(--qd-accent)",
      "Meeting transcripts": "var(--qd-profit)",
      "Decisions": "var(--qd-warn)",
      "Projects": "var(--qd-loss)",
      "Trading Rules": "rgb(168, 85, 247)", // Purple
    };
    return map[topic] || "var(--qd-text-3)";
  };

  return (
    <div className="qd-card p-3 bg-[var(--qd-surface)] border border-[var(--qd-border)] relative">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--qd-text-3)] mb-2 flex items-center justify-between">
        <span>Knowledge Map (Obsidian view)</span>
        {hoveredNode && <span className="text-[var(--qd-accent)] truncate max-w-[180px]">{hoveredNode}</span>}
      </div>
      <svg
        ref={canvasRef}
        width="100%"
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className="bg-[var(--qd-surface-2)] rounded-[var(--qd-radius-sm)] cursor-grab"
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
      >
        {/* Render links */}
        {links.map((link, i) => {
          const s = nodes.find((n) => n.id === link.source);
          const t = nodes.find((n) => n.id === link.target);
          if (!s || !t) return null;
          
          const isHighlighted = s.id === currentTitle || t.id === currentTitle;
          return (
            <line
              key={i}
              x1={s.x}
              y1={s.y}
              x2={t.x}
              y2={t.y}
              stroke={isHighlighted ? "var(--qd-accent)" : "var(--qd-border)"}
              strokeWidth={isHighlighted ? 1.5 : 1}
              strokeOpacity={isHighlighted ? 0.8 : 0.4}
            />
          );
        })}

        {/* Render nodes */}
        {nodes.map((node) => {
          const isCurrent = node.id === currentTitle;
          const isLinked = links.some(
            (l) =>
              (l.source === currentTitle && l.target === node.id) ||
              (l.target === currentTitle && l.source === node.id)
          );
          
          return (
            <g
              key={node.id}
              className="transition-opacity duration-150"
              style={{ opacity: hoveredNode && hoveredNode !== node.id && !isLinked && !isCurrent ? 0.3 : 1 }}
              onMouseEnter={() => setHoveredNode(node.id)}
              onMouseLeave={() => setHoveredNode(null)}
              onClick={() => onSelectNode(node.id)}
            >
              <circle
                cx={node.x}
                cy={node.y}
                r={isCurrent ? 7 : 5}
                fill={getTopicColor(node.topic)}
                stroke={isCurrent ? "var(--qd-text)" : "transparent"}
                strokeWidth={2}
                className="cursor-pointer"
                onMouseDown={(e) => handleMouseDown(node, e)}
              />
              {isCurrent && (
                <circle
                  cx={node.x}
                  cy={node.y}
                  r={12}
                  fill="none"
                  stroke="var(--qd-accent)"
                  strokeWidth={1}
                  className="animate-pulse"
                />
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// ==================== Main Page Component ====================
export default function Wiki() {
  const [notes, setNotes] = useState([]);
  const [graphData, setGraphData] = useState({ nodes: [], links: [] });
  const [selectedNote, setSelectedNote] = useState(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [isEditing, setIsEditing] = useState(false);
  const [syncBusy, setSyncBusy] = useState(false);
  const [saveBusy, setSaveBusy] = useState(false);
  
  // Accordion toggle states per topic
  const [expandedTopics, setExpandedTopics] = useState({
    "YouTube transcripts": true,
    "Meeting transcripts": true,
    "Decisions": true,
    "Projects": true,
    "Trading Rules": true,
    "General": true,
  });

  // Editor states
  const [editTitle, setEditTitle] = useState("");
  const [editTopic, setEditTopic] = useState("General");
  const [editTags, setEditTags] = useState("");
  const [editUrl, setEditUrl] = useState("");
  const [editDate, setEditDate] = useState("");
  const [editContent, setEditContent] = useState("");

  const topicsList = [
    "YouTube transcripts",
    "Meeting transcripts",
    "Decisions",
    "Projects",
    "Trading Rules",
    "General",
  ];

  const fetchNotesAndGraph = async (autoSelectTitle = null) => {
    try {
      const [listRes, graphRes] = await Promise.all([
        api.get("/wiki"),
        api.get("/wiki/graph/data")
      ]);
      setNotes(listRes.data);
      setGraphData(graphRes.data);

      if (autoSelectTitle) {
        // Find by exact title match
        const found = listRes.data.find(n => n.title.toLowerCase() === autoSelectTitle.toLowerCase());
        if (found) {
          fetchSingleNote(found.id);
          return;
        }
      }

      // Default selection (first note in list if none selected)
      if (listRes.data.length > 0 && !selectedNote && !isEditing) {
        fetchSingleNote(listRes.data[0].id);
      }
    } catch (e) {
      toast.error("Failed to load Knowledge Hub data");
    }
  };

  const fetchSingleNote = async (idOrTitle) => {
    try {
      const res = await api.get(`/wiki/${encodeURIComponent(idOrTitle)}`);
      setSelectedNote(res.data);
      setIsEditing(false);
    } catch (e) {
      toast.error("Note not found or could not be loaded");
    }
  };

  useEffect(() => {
    fetchNotesAndGraph();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync Obsidian directory with database
  const handleSyncObsidian = async () => {
    setSyncBusy(true);
    const toastId = toast.loading("Scanning Obsidian files...");
    try {
      const res = await api.post("/wiki/sync");
      toast.success(res.data.message, { id: toastId });
      fetchNotesAndGraph(selectedNote?.title);
    } catch (e) {
      toast.error("Sync failed", { id: toastId });
    } finally {
      setSyncBusy(false);
    }
  };

  // Start new note creator
  const handleCreateNew = () => {
    setIsEditing(true);
    setSelectedNote(null);
    setEditTitle("");
    setEditTopic("General");
    setEditTags("");
    setEditUrl("");
    setEditDate(new Date().toISOString().split("T")[0]);
    setEditContent("");
  };

  // Start editor for current note
  const handleEditNote = () => {
    if (!selectedNote) return;
    setIsEditing(true);
    setEditTitle(selectedNote.title);
    setEditTopic(selectedNote.topic);
    setEditTags(selectedNote.tags.join(", "));
    setEditUrl(selectedNote.metadata?.url || "");
    setEditDate(selectedNote.metadata?.date || new Date().toISOString().split("T")[0]);
    setEditContent(selectedNote.content);
  };

  // Save/Update note
  const handleSave = async (e) => {
    e.preventDefault();
    if (!editTitle.trim()) {
      toast.error("Title is required");
      return;
    }

    setSaveBusy(true);
    const tagsArr = editTags.split(",").map(t => t.trim()).filter(Boolean);
    const metadataObj = {};
    if (editUrl) metadataObj.url = editUrl;
    if (editDate) metadataObj.date = editDate;

    const payload = {
      title: editTitle.trim(),
      topic: editTopic,
      content: editContent,
      tags: tagsArr,
      metadata: metadataObj
    };

    try {
      if (selectedNote) {
        // Update
        const res = await api.put(`/wiki/${selectedNote.id}`, payload);
        toast.success("Note saved successfully");
        setSelectedNote(res.data);
      } else {
        // Create
        const res = await api.post("/wiki", payload);
        toast.success("Note created successfully");
        setSelectedNote(res.data);
      }
      setIsEditing(false);
      fetchNotesAndGraph(editTitle.trim());
    } catch (err) {
      toast.error(err.response?.data?.detail || "Failed to save note");
    } finally {
      setSaveBusy(false);
    }
  };

  // Delete note
  const handleDelete = async () => {
    if (!selectedNote) return;
    if (!window.confirm("Are you sure you want to delete this note? This deletes the local file too.")) return;

    try {
      await api.delete(`/wiki/${selectedNote.id}`);
      toast.success("Note deleted");
      setSelectedNote(null);
      fetchNotesAndGraph();
    } catch (e) {
      toast.error("Deletion failed");
    }
  };

  const toggleTopic = (topic) => {
    setExpandedTopics(prev => ({
      ...prev,
      [topic]: !prev[topic]
    }));
  };

  // Filter notes by search query
  const filteredNotes = notes.filter(n => {
    const q = searchQuery.toLowerCase();
    return (
      n.title.toLowerCase().includes(q) ||
      n.topic.toLowerCase().includes(q) ||
      n.tags.some(t => t.toLowerCase().includes(q))
    );
  });

  // Group filtered notes by topic
  const groupedNotes = filteredNotes.reduce((acc, note) => {
    const topic = note.topic || "General";
    if (!acc[topic]) acc[topic] = [];
    acc[topic].push(note);
    return acc;
  }, {});

  return (
    <div className="space-y-4">
      {/* Top Header Card */}
      <div className="qd-card qd-hero-panel p-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <BookOpen className="text-[var(--qd-accent)]" size={20} />
            <h1 className="font-head text-2xl font-extrabold text-[var(--qd-text)]">Knowledge Hub & Second Brain</h1>
          </div>
          <p className="text-xs text-[var(--qd-text-2)] mt-1.5 leading-relaxed max-w-2xl">
            A centralized wiki vault holding transcripts, rules, and notes. Link topics with double-brackets (e.g. <code>[[Title]]</code>)
            to build backlinks, and sync with your local Obsidian workspace live.
          </p>
        </div>
        <div className="flex items-center gap-2 self-start md:self-auto">
          <Button
            variant="secondary"
            size="sm"
            onClick={handleSyncObsidian}
            disabled={syncBusy}
            data-testid="wiki-sync"
          >
            <RefreshCw size={13} className={syncBusy ? "animate-spin mr-1" : "mr-1"} />
            Sync Obsidian
          </Button>
          <Button variant="accent" size="sm" onClick={handleCreateNew} data-testid="wiki-new">
            <Plus size={14} className="mr-1" /> New Note
          </Button>
        </div>
      </div>

      {/* Main Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
        {/* Left Side: Topic Tree Selector (Col 3) */}
        <div className="lg:col-span-3 qd-card p-3 space-y-3 bg-[var(--qd-surface)] border border-[var(--qd-border)] min-h-[500px]">
          <div className="relative">
            <Search className="absolute left-2.5 top-2.5 text-[var(--qd-text-3)]" size={14} />
            <input
              type="text"
              placeholder="Search concepts or tags..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] pl-8 pr-3 py-2 text-xs text-[var(--qd-text)] placeholder-[var(--qd-text-3)] focus:outline-none focus:border-[var(--qd-accent)]"
            />
          </div>

          <div className="space-y-2 overflow-y-auto max-h-[600px] pr-1">
            {topicsList.map((topic) => {
              const notesInTopic = groupedNotes[topic] || [];
              const isExpanded = expandedTopics[topic];
              if (searchQuery && notesInTopic.length === 0) return null;

              return (
                <div key={topic} className="border-b border-[var(--qd-border)] pb-2 last:border-0">
                  <button
                    type="button"
                    onClick={() => toggleTopic(topic)}
                    className="w-full flex items-center justify-between text-left text-xs font-bold font-head uppercase tracking-wider text-[var(--qd-text-2)] hover:text-[var(--qd-text)] py-1.5"
                  >
                    <div className="flex items-center gap-1.5">
                      <FolderOpen size={13} className="text-[var(--qd-accent)]" />
                      <span>{topic}</span>
                      <span className="font-mono text-[9px] text-[var(--qd-text-3)] bg-[var(--qd-surface-2)] px-1 rounded">
                        {notesInTopic.length}
                      </span>
                    </div>
                    {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  </button>

                  {isExpanded && (
                    <div className="mt-1 pl-3 space-y-1">
                      {notesInTopic.length === 0 ? (
                        <div className="text-[10px] text-[var(--qd-text-3)] italic py-1">No notes</div>
                      ) : (
                        notesInTopic.map((note) => {
                          const isSelected = selectedNote?.id === note.id;
                          return (
                            <button
                              key={note.id}
                              type="button"
                              onClick={() => fetchSingleNote(note.id)}
                              className={`w-full text-left text-xs py-1.5 px-2 rounded-[var(--qd-radius-sm)] flex items-center gap-2 truncate transition-colors ${
                                isSelected
                                  ? "bg-[var(--qd-surface-3)] text-[var(--qd-text)] border border-[var(--qd-border-strong)]"
                                  : "text-[var(--qd-text-2)] hover:bg-[var(--qd-surface-2)] hover:text-[var(--qd-text)] border border-transparent"
                              }`}
                            >
                              <FileText size={12} className="shrink-0" />
                              <span className="truncate">{note.title}</span>
                            </button>
                          );
                        })
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Center: Viewer / Editor Panel (Col 6) */}
        <div className="lg:col-span-6 qd-card p-4 bg-[var(--qd-surface)] border border-[var(--qd-border)] min-h-[500px]">
          {isEditing ? (
            /* Note Editor Form */
            <form onSubmit={handleSave} className="space-y-4">
              <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-3">
                <h2 className="font-head text-md font-bold text-[var(--qd-text)]">
                  {selectedNote ? "Edit Note" : "Create New Note"}
                </h2>
                <div className="flex gap-2">
                  <Button variant="secondary" size="sm" type="button" onClick={() => {
                    setIsEditing(false);
                    if (!selectedNote && notes.length > 0) fetchSingleNote(notes[0].id);
                  }}>
                    Cancel
                  </Button>
                  <Button variant="accent" size="sm" type="submit" disabled={saveBusy} data-testid="wiki-save">
                    {saveBusy ? "Saving..." : "Save Note"}
                  </Button>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5 col-span-2">
                  <label className="text-[10px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Note Title</label>
                  <input
                    type="text"
                    required
                    placeholder="e.g. Upstox API Keys Setup"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] px-3 py-2 text-sm text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)]"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-[10px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Topic Folder</label>
                  <select
                    value={editTopic}
                    onChange={(e) => setEditTopic(e.target.value)}
                    className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] px-3 py-2 text-xs text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)]"
                  >
                    {topicsList.map(t => (
                      <option key={t} value={t}>{t}</option>
                    ))}
                  </select>
                </div>

                <div className="space-y-1.5">
                  <label className="text-[10px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Tags (comma separated)</label>
                  <input
                    type="text"
                    placeholder="e.g. guide, upstox, keys"
                    value={editTags}
                    onChange={(e) => setEditTags(e.target.value)}
                    className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] px-3 py-2 text-xs text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)]"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-[10px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Reference URL (optional)</label>
                  <input
                    type="url"
                    placeholder="e.g. http://youtube.com/..."
                    value={editUrl}
                    onChange={(e) => setEditUrl(e.target.value)}
                    className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] px-3 py-2 text-xs text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)]"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-[10px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Date (optional)</label>
                  <input
                    type="date"
                    value={editDate}
                    onChange={(e) => setEditDate(e.target.value)}
                    className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] px-3 py-2 text-xs text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)]"
                  />
                </div>
              </div>

              <div className="space-y-1.5">
                <div className="flex justify-between items-center">
                  <label className="text-[10px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Markdown Content</label>
                  <span className="text-[9px] text-[var(--qd-text-3)] font-mono">Use [[Page Title]] to link pages</span>
                </div>
                <textarea
                  rows={16}
                  placeholder="# Note Title&#10;Write note content here. Standard markdown list syntax:&#10;- Bullet item&#10;Use [[Upstox Setup]] to cross link concepts."
                  value={editContent}
                  onChange={(e) => setEditContent(e.target.value)}
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] p-3 text-xs font-mono text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)] resize-y"
                />
              </div>
            </form>
          ) : selectedNote ? (
            /* Note Reader View */
            <div className="space-y-4">
              {/* Toolbar */}
              <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-3">
                <div className="flex items-center gap-1.5 bg-[var(--qd-surface-2)] px-2 py-1 rounded text-[10px] font-bold text-[var(--qd-accent)] uppercase font-mono tracking-wider">
                  <FolderOpen size={11} /> {selectedNote.topic}
                </div>
                <div className="flex gap-2">
                  <Button variant="ghost" size="icon" onClick={handleEditNote} title="Edit note" data-testid="wiki-edit">
                    <Edit2 size={14} />
                  </Button>
                  <Button variant="ghost" size="icon" onClick={handleDelete} className="hover:text-[var(--qd-loss)]" title="Delete note" data-testid="wiki-delete">
                    <Trash2 size={14} />
                  </Button>
                </div>
              </div>

              {/* Title & Metadata */}
              <div>
                <h1 className="font-head text-3xl font-extrabold text-[var(--qd-text)] leading-tight">
                  {selectedNote.title}
                </h1>
                
                {/* Meta details list */}
                <div className="flex flex-wrap gap-x-4 gap-y-2 mt-3 text-xs text-[var(--qd-text-3)] border-b border-[var(--qd-border)] pb-3">
                  {selectedNote.metadata?.date && (
                    <div className="flex items-center gap-1">
                      <Calendar size={13} />
                      <span>{selectedNote.metadata.date}</span>
                    </div>
                  )}
                  {selectedNote.metadata?.url && (
                    <div className="flex items-center gap-1 truncate max-w-[250px]">
                      <ExternalLink size={13} />
                      <a href={selectedNote.metadata.url} target="_blank" rel="noopener noreferrer" className="text-[var(--qd-accent)] hover:underline truncate">
                        {selectedNote.metadata.url}
                      </a>
                    </div>
                  )}
                  {selectedNote.tags && selectedNote.tags.length > 0 && (
                    <div className="flex items-center gap-1 flex-wrap">
                      <Tag size={13} />
                      {selectedNote.tags.map(tag => (
                        <span key={tag} className="bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] text-[10px] px-1.5 py-0.5 rounded">
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              {/* Content Body */}
              <div className="prose max-w-none text-[var(--qd-text-2)] text-sm pb-6 min-h-[250px]">
                {renderMarkdownContent(selectedNote.content, fetchSingleNote)}
              </div>

              {/* Backlinks / "See Also" footer section */}
              {selectedNote.backlinks && selectedNote.backlinks.length > 0 && (
                <div className="border-t border-[var(--qd-border)] pt-4 mt-6">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-[var(--qd-text-3)] flex items-center gap-1.5 mb-2.5">
                    <Share2 size={11} />
                    <span>See Also (Linked Mentions)</span>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {selectedNote.backlinks.map(referrer => (
                      <button
                        key={referrer}
                        type="button"
                        onClick={() => fetchSingleNote(referrer)}
                        className="text-left text-xs p-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] hover:text-[var(--qd-text)] hover:border-[var(--qd-border-strong)] transition-colors truncate"
                      >
                        {referrer}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            /* Landing state when empty */
            <div className="h-full flex flex-col items-center justify-center text-[var(--qd-text-3)] text-center space-y-3">
              <BookOpen size={48} className="stroke-[1] text-[var(--qd-border-strong)]" />
              <div>
                <h3 className="font-head font-bold text-sm text-[var(--qd-text-2)]">No Note Selected</h3>
                <p className="text-xs max-w-xs mt-1">Select a transcript or rule file in the left sidebar, or create a new note to start building your second brain.</p>
              </div>
            </div>
          )}
        </div>

        {/* Right Side: link graph & topics summary (Col 3) */}
        <div className="lg:col-span-3 space-y-4">
          {/* Interactive Graph view component */}
          <WikiGraph
            data={graphData}
            currentTitle={selectedNote?.title}
            onSelectNode={fetchSingleNote}
          />

          {/* Quick instructions panel */}
          <div className="qd-card p-3 space-y-2 bg-[var(--qd-surface)] border border-[var(--qd-border)]">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--qd-text-3)]">
              Wiki Guidelines
            </div>
            <ul className="text-[10px] leading-relaxed text-[var(--qd-text-2)] list-disc pl-4 space-y-1">
              <li>Organize transcripts and decisions by topic to help the AI contextualize.</li>
              <li>Always link concepts using double brackets like <code>[[Page Title]]</code> to automatically build relationship chains.</li>
              <li>Edits made outside this workspace inside Obsidian are fully synced using the <strong>Sync Obsidian</strong> button.</li>
              <li>A persistent auto-memory ledger is maintained in <code>wiki/memory.md</code> for multi-agent session handovers.</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
