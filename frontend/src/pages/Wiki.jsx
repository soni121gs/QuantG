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
import MarkdownRenderer from "../components/wiki/MarkdownRenderer";
import PhysicsGraphCanvas from "../components/wiki/PhysicsGraphCanvas";
import WikiTreeSidebar from "../components/wiki/WikiTreeSidebar";



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
        <WikiTreeSidebar
          searchQuery={searchQuery}
          onSearchChange={setSearchQuery}
          topicsList={topicsList}
          groupedNotes={groupedNotes}
          expandedTopics={expandedTopics}
          onToggleTopic={toggleTopic}
          selectedNote={selectedNote}
          onSelectNote={fetchSingleNote}
        />

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
                <MarkdownRenderer content={selectedNote.content} onSelectNote={fetchSingleNote} />
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
          <PhysicsGraphCanvas
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
