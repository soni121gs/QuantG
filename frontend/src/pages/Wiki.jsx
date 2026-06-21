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
  
  // Autocomplete states & refs
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [suggestions, setSuggestions] = useState([]);
  const [activeSuggestion, setActiveSuggestion] = useState(0);
  const [triggerIndex, setTriggerIndex] = useState(-1);
  const textareaRef = useRef(null);

  // Unlinked Mentions states
  const [unlinkedMentions, setUnlinkedMentions] = useState([]);
  const [activeFooterTab, setActiveFooterTab] = useState("backlinks");

  // Trading Rules Audit state
  const [auditResult, setAuditResult] = useState(null);

  // AI Summarize states
  const [showSummarizeModal, setShowSummarizeModal] = useState(false);
  const [summarizeUrlInput, setSummarizeUrlInput] = useState("");
  const [summarizeBusy, setSummarizeBusy] = useState(false);

  const topicsList = [
    "YouTube transcripts",
    "Meeting transcripts",
    "Decisions",
    "Projects",
    "Trading Rules",
    "General",
  ];

  const fetchAuditResult = async () => {
    try {
      const res = await api.get("/wiki/audit/rules");
      setAuditResult(res.data);
    } catch (err) {
      console.error("Failed to load trading rules audit", err);
    }
  };

  const fetchNotesAndGraph = async (autoSelectTitle = null) => {
    try {
      const [listRes, graphRes] = await Promise.all([
        api.get("/wiki"),
        api.get("/wiki/graph/data")
      ]);
      setNotes(listRes.data);
      setGraphData(graphRes.data);
      fetchAuditResult();

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
      setActiveFooterTab("backlinks");
      
      // Fetch unlinked mentions
      try {
        const mentionsRes = await api.get(`/wiki/${encodeURIComponent(idOrTitle)}/unlinked-mentions`);
        setUnlinkedMentions(mentionsRes.data);
      } catch (err) {
        console.error("Failed to load unlinked mentions", err);
        setUnlinkedMentions([]);
      }
    } catch (e) {
      toast.error("Note not found or could not be loaded");
    }
  };

  const handleLinkMention = async (sourceId) => {
    if (!selectedNote) return;
    const toastId = toast.loading("Creating wikilink reference...");
    try {
      const res = await api.post(`/wiki/${encodeURIComponent(selectedNote.id)}/link-mention`, {
        source_id: sourceId
      });
      toast.success(res.data.message, { id: toastId });
      // Refresh
      fetchSingleNote(selectedNote.id);
      fetchNotesAndGraph(selectedNote.title);
    } catch (err) {
      toast.error(err.response?.data?.detail || "Failed to link mention", { id: toastId });
    }
  };

  const handleSummarizeUrl = async (e) => {
    e.preventDefault();
    if (!summarizeUrlInput.trim()) {
      toast.error("Please enter a valid URL");
      return;
    }
    setSummarizeBusy(true);
    const toastId = toast.loading("Fetching page and summarizing via AI...");
    try {
      const res = await api.post("/wiki/summarize", {
        url: summarizeUrlInput.trim()
      });
      toast.success("Webpage summarized successfully", { id: toastId });
      
      // Load directly into Editor
      setIsEditing(true);
      setSelectedNote(null);
      setEditTitle(res.data.title || "Imported Note");
      setEditTopic(res.data.topic || "General");
      setEditTags((res.data.tags || []).join(", "));
      setEditUrl(summarizeUrlInput.trim());
      setEditDate(new Date().toISOString().split("T")[0]);
      setEditContent(res.data.summary_markdown || "");
      
      setSummarizeUrlInput("");
      setShowSummarizeModal(false);
    } catch (err) {
      toast.error(err.response?.data?.detail || "Failed to summarize webpage", { id: toastId });
    } finally {
      setSummarizeBusy(false);
    }
  };

  useEffect(() => {
    fetchNotesAndGraph();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Debounced search effect
  useEffect(() => {
    // Skip initial run on mount since fetchNotesAndGraph does it
    const isFirstRun = searchQuery === "";
    if (isFirstRun) return;

    const delayDebounce = setTimeout(() => {
      const fetchFilteredNotes = async () => {
        try {
          const res = await api.get(
            searchQuery.trim()
              ? `/wiki?search=${encodeURIComponent(searchQuery.trim())}`
              : "/wiki"
          );
          setNotes(res.data);
        } catch (e) {
          toast.error("Search failed");
        }
      };
      fetchFilteredNotes();
    }, 250);

    return () => clearTimeout(delayDebounce);
  }, [searchQuery]);

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

  // Autocomplete suggestions handlers
  const handleTextareaChange = (e) => {
    const val = e.target.value;
    setEditContent(val);
    const cursor = e.target.selectionStart;
    
    const lastOpen = val.lastIndexOf("[[", cursor - 1);
    if (lastOpen !== -1) {
      const textSinceOpen = val.substring(lastOpen + 2, cursor);
      const hasClose = textSinceOpen.includes("]]");
      const hasNewline = textSinceOpen.includes("\n");
      
      if (!hasClose && !hasNewline) {
        const searchWord = textSinceOpen.toLowerCase();
        const matches = notes
          .filter(n => n.title.toLowerCase().includes(searchWord))
          .map(n => n.title);
        
        if (matches.length > 0) {
          setSuggestions(matches);
          setShowSuggestions(true);
          setActiveSuggestion(0);
          setTriggerIndex(lastOpen);
          return;
        }
      }
    }
    setShowSuggestions(false);
  };

  const handleTextareaKeyDown = (e) => {
    if (!showSuggestions) return;
    
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveSuggestion(prev => (prev + 1) % suggestions.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveSuggestion(prev => (prev - 1 + suggestions.length) % suggestions.length);
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      selectSuggestion(activeSuggestion);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setShowSuggestions(false);
    }
  };

  const selectSuggestion = (index) => {
    const selectedTitle = suggestions[index];
    const textarea = textareaRef.current;
    if (!textarea) return;
    
    const val = editContent;
    const currentCursor = textarea.selectionStart;
    const before = val.substring(0, triggerIndex);
    const after = val.substring(currentCursor);
    const replacement = `[[${selectedTitle}]]`;
    const newVal = before + replacement + after;
    
    setEditContent(newVal);
    setShowSuggestions(false);
    
    setTimeout(() => {
      textarea.focus();
      const newCursorPos = triggerIndex + replacement.length;
      textarea.setSelectionRange(newCursorPos, newCursorPos);
    }, 0);
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

  // Filtered by backend search query parameter
  const filteredNotes = notes;

  // Group filtered notes by topic
  const groupedNotes = filteredNotes.reduce((acc, note) => {
    const topic = note.topic || "General";
    if (!acc[topic]) acc[topic] = [];
    acc[topic].push(note);
    return acc;
  }, {});

  return (
    <div className="space-y-3">
      {/* Top Header Card */}
      <div className="qd-card qd-hero-panel p-3 flex flex-col md:flex-row md:items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <BookOpen className="text-[var(--qd-accent)]" size={18} />
            <h1 className="font-head text-xl font-extrabold text-[var(--qd-text)]">Knowledge Hub & Second Brain</h1>
          </div>
          <p className="text-[10.5px] text-[var(--qd-text-2)] mt-1 leading-relaxed max-w-2xl">
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
            className="h-8 text-xs px-2.5"
          >
            <RefreshCw size={12} className={syncBusy ? "animate-spin mr-1" : "mr-1"} />
            Sync Obsidian
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setShowSummarizeModal(true)}
            data-testid="wiki-summarize-btn"
            className="h-8 text-xs px-2.5"
          >
            <Share2 size={12} className="mr-1 rotate-90" /> AI Summarize
          </Button>
          <Button variant="accent" size="sm" onClick={handleCreateNew} data-testid="wiki-new" className="h-8 text-xs px-2.5">
            <Plus size={13} className="mr-1" /> New Note
          </Button>
        </div>
      </div>

      {/* Main Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-3">
        <WikiTreeSidebar
          searchQuery={searchQuery}
          onSearchChange={setSearchQuery}
          topicsList={topicsList}
          groupedNotes={groupedNotes}
          expandedTopics={expandedTopics}
          onToggleTopic={toggleTopic}
          selectedNote={selectedNote}
          onSelectNote={fetchSingleNote}
          auditResult={auditResult}
        />

        {/* Center: Viewer / Editor Panel (Col 6) */}
        <div className="lg:col-span-6 qd-card p-3 bg-[var(--qd-surface)] border border-[var(--qd-border)] min-h-[500px]">
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
                  <label className="text-[11px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Note Title</label>
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
                  <label className="text-[11px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Topic Folder</label>
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
                  <label className="text-[11px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Tags (comma separated)</label>
                  <input
                    type="text"
                    placeholder="e.g. guide, upstox, keys"
                    value={editTags}
                    onChange={(e) => setEditTags(e.target.value)}
                    className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] px-3 py-2 text-xs text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)]"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-[11px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Reference URL (optional)</label>
                  <input
                    type="url"
                    placeholder="e.g. http://youtube.com/..."
                    value={editUrl}
                    onChange={(e) => setEditUrl(e.target.value)}
                    className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] px-3 py-2 text-xs text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)]"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-[11px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Date (optional)</label>
                  <input
                    type="date"
                    value={editDate}
                    onChange={(e) => setEditDate(e.target.value)}
                    className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] px-3 py-2 text-xs text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)]"
                  />
                </div>
              </div>

              <div className="space-y-1.5 relative">
                <div className="flex justify-between items-center">
                  <label className="text-[11px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">Markdown Content</label>
                  <span className="text-[11px] text-[var(--qd-text-3)] font-mono">Use [[Page Title]] to link pages</span>
                </div>
                <textarea
                  ref={textareaRef}
                  rows={16}
                  placeholder="# Note Title&#10;Write note content here. Standard markdown list syntax:&#10;- Bullet item&#10;Use [[Upstox Setup]] to cross link concepts."
                  value={editContent}
                  onChange={handleTextareaChange}
                  onKeyDown={handleTextareaKeyDown}
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] p-3 text-xs font-mono text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)] resize-y"
                />

                {/* Suggestions overlay */}
                {showSuggestions && (
                  <div className="absolute z-50 left-3 bottom-full mb-1 max-h-48 w-64 overflow-y-auto bg-[var(--qd-surface-3)] border border-[var(--qd-border-strong)] rounded-[var(--qd-radius-sm)] shadow-lg p-1">
                    {suggestions.map((title, idx) => (
                      <button
                        key={title}
                        type="button"
                        onClick={() => selectSuggestion(idx)}
                        className={`w-full text-left text-xs px-2.5 py-1.5 rounded-[var(--qd-radius-xs)] font-sans truncate transition-colors ${
                          idx === activeSuggestion
                            ? "bg-[var(--qd-accent)] text-white font-bold"
                            : "text-[var(--qd-text-2)] hover:bg-[var(--qd-surface-2)]"
                        }`}
                      >
                        {title}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </form>
          ) : selectedNote ? (
            /* Note Reader View */
            <div className="space-y-4">
              {/* Toolbar */}
              <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-3">
                <div className="flex items-center gap-1.5 bg-[var(--qd-surface-2)] px-2 py-1 rounded text-[11px] font-bold text-[var(--qd-accent)] uppercase font-mono tracking-wider">
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

              {/* Drift Auditor Warnings Alert Banner */}
              {selectedNote.topic === "Trading Rules" && auditResult?.status === "warning" && auditResult?.mismatches?.length > 0 && (
                <div className="bg-[rgba(234,179,8,0.1)] border border-[var(--qd-warn)] rounded-[var(--qd-radius-sm)] p-3 text-xs space-y-2">
                  <div className="flex items-center gap-2 text-[var(--qd-warn)] font-bold font-head uppercase tracking-wider">
                    <svg className="h-4 w-4 animate-bounce shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                    </svg>
                    <span>Trading Rules Mismatch Detected</span>
                  </div>
                  <p className="text-[var(--qd-text-2)] leading-relaxed font-sans">
                    Your active system boundaries deviate from the documented guidelines in your wiki.
                  </p>
                  <div className="border-t border-[rgba(234,179,8,0.2)] pt-2 space-y-1.5 font-sans">
                    {auditResult.mismatches.map((m, idx) => (
                      <div key={idx} className="flex flex-col md:flex-row md:items-center justify-between text-[11px] bg-[rgba(0,0,0,0.15)] p-1.5 rounded gap-1 border border-[rgba(234,179,8,0.1)]">
                        <span className="font-semibold text-[var(--qd-text)]">{m.rule}:</span>
                        <div className="flex items-center gap-2">
                          <span className="text-[var(--qd-text-3)] font-mono line-through">Doc: {m.documented}</span>
                          <span className="text-[var(--qd-warn)] font-mono font-bold">Active: {m.active}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

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
                        <span key={tag} className="bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] text-[11px] px-1.5 py-0.5 rounded">
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

              {/* Backlinks & Unlinked Mentions Tabs Footer */}
              {((selectedNote.backlinks && selectedNote.backlinks.length > 0) || (unlinkedMentions && unlinkedMentions.length > 0)) && (
                <div className="border-t border-[var(--qd-border)] pt-4 mt-6">
                  {/* Tab Selector */}
                  <div className="flex border-b border-[var(--qd-border)] mb-3 text-xs">
                    <button
                      type="button"
                      onClick={() => setActiveFooterTab("backlinks")}
                      className={`pb-2 px-3 font-semibold uppercase tracking-wider transition-colors border-b-2 ${
                        activeFooterTab === "backlinks"
                          ? "border-[var(--qd-accent)] text-[var(--qd-text)]"
                          : "border-transparent text-[var(--qd-text-3)] hover:text-[var(--qd-text-2)]"
                      }`}
                    >
                      Linked Mentions ({selectedNote.backlinks?.length || 0})
                    </button>
                    <button
                      type="button"
                      onClick={() => setActiveFooterTab("unlinked")}
                      className={`pb-2 px-3 font-semibold uppercase tracking-wider transition-colors border-b-2 ${
                        activeFooterTab === "unlinked"
                          ? "border-[var(--qd-accent)] text-[var(--qd-text)]"
                          : "border-transparent text-[var(--qd-text-3)] hover:text-[var(--qd-text-2)]"
                      }`}
                    >
                      Unlinked Mentions ({unlinkedMentions?.length || 0})
                    </button>
                  </div>

                  {activeFooterTab === "backlinks" && (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                      {selectedNote.backlinks && selectedNote.backlinks.length > 0 ? (
                        selectedNote.backlinks.map(referrer => (
                          <button
                            key={referrer}
                            type="button"
                            onClick={() => fetchSingleNote(referrer)}
                            className="text-left text-xs p-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] hover:text-[var(--qd-text)] hover:border-[var(--qd-border-strong)] transition-colors truncate"
                          >
                            {referrer}
                          </button>
                        ))
                      ) : (
                        <div className="text-xs text-[var(--qd-text-3)] italic p-2 col-span-2">No linked references.</div>
                      )}
                    </div>
                  )}

                  {activeFooterTab === "unlinked" && (
                    <div className="space-y-2">
                      {unlinkedMentions && unlinkedMentions.length > 0 ? (
                        unlinkedMentions.map(mention => (
                          <div
                            key={mention.id}
                            className="flex items-center justify-between text-xs p-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] hover:border-[var(--qd-border-strong)] transition-colors"
                          >
                            <span className="truncate font-sans font-medium">
                              {mention.title} <span className="text-[10px] text-[var(--qd-text-3)]">({mention.topic})</span>
                            </span>
                            <button
                              type="button"
                              className="h-6 text-[10px] bg-[var(--qd-surface-3)] border border-[var(--qd-border)] rounded px-2 hover:bg-[var(--qd-surface)] font-medium text-[var(--qd-text-2)] transition-colors"
                              onClick={() => handleLinkMention(mention.id)}
                            >
                              Link Mention
                            </button>
                          </div>
                        ))
                      ) : (
                        <div className="text-xs text-[var(--qd-text-3)] italic p-2">No unlinked mentions found in other files.</div>
                      )}
                    </div>
                  )}
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
            <div className="text-[11px] font-semibold uppercase tracking-wider text-[var(--qd-text-3)]">
              Wiki Guidelines
            </div>
            <ul className="text-[11px] leading-relaxed text-[var(--qd-text-2)] list-disc pl-4 space-y-1">
              <li>Organize transcripts and decisions by topic to help the AI contextualize.</li>
              <li>Always link concepts using double brackets like <code>[[Page Title]]</code> to automatically build relationship chains.</li>
              <li>Edits made outside this workspace inside Obsidian are fully synced using the <strong>Sync Obsidian</strong> button.</li>
              <li>A persistent auto-memory ledger is maintained in <code>wiki/memory.md</code> for multi-agent session handovers.</li>
            </ul>
          </div>
        </div>
      </div>

      {/* AI Summarize Url Modal Popup */}
      {showSummarizeModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4">
          <div className="w-full max-w-md bg-[var(--qd-surface)] border border-[var(--qd-border-strong)] rounded-[var(--qd-radius-sm)] shadow-2xl p-5 space-y-4">
            <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-2.5">
              <h3 className="font-head font-bold text-sm text-[var(--qd-text)] flex items-center gap-2">
                <Share2 size={16} className="text-[var(--qd-accent)] rotate-90" />
                AI Webpage Summarizer
              </h3>
              <button
                type="button"
                className="text-[var(--qd-text-3)] hover:text-[var(--qd-text)] text-xs"
                onClick={() => {
                  setSummarizeUrlInput("");
                  setShowSummarizeModal(false);
                }}
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleSummarizeUrl} className="space-y-4">
              <div className="space-y-1.5">
                <label className="text-[10px] font-bold uppercase tracking-wider text-[var(--qd-text-3)]">
                  Enter Webpage or Video URL
                </label>
                <input
                  type="url"
                  required
                  placeholder="e.g. https://www.youtube.com/watch?v=..."
                  value={summarizeUrlInput}
                  onChange={(e) => setSummarizeUrlInput(e.target.value)}
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] px-3 py-2 text-xs text-[var(--qd-text)] focus:outline-none focus:border-[var(--qd-accent)]"
                  disabled={summarizeBusy}
                />
                <p className="text-[10px] text-[var(--qd-text-3)] leading-relaxed">
                  Fetches the webpage transcript, extracts core trading models, and drafts a structured Markdown note for you.
                </p>
              </div>

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => {
                    setSummarizeUrlInput("");
                    setShowSummarizeModal(false);
                  }}
                  className="px-3 py-1.5 rounded-[var(--qd-radius-xs)] text-xs border border-[var(--qd-border)] text-[var(--qd-text-2)] hover:bg-[var(--qd-surface-2)]"
                  disabled={summarizeBusy}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-4 py-1.5 bg-[var(--qd-accent)] hover:opacity-90 transition-opacity text-white rounded-[var(--qd-radius-xs)] text-xs font-bold"
                  disabled={summarizeBusy}
                >
                  {summarizeBusy ? "Processing..." : "Summarize URL"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
