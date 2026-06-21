import React from "react";
import { Search, FolderOpen, ChevronDown, ChevronRight, FileText, AlertTriangle } from "lucide-react";

export default function WikiTreeSidebar({
  searchQuery,
  onSearchChange,
  topicsList,
  groupedNotes,
  expandedTopics,
  onToggleTopic,
  selectedNote,
  onSelectNote,
  auditResult,
}) {
  return (
    <div className="lg:col-span-3 qd-card p-2.5 space-y-2.5 bg-[var(--qd-surface)] border border-[var(--qd-border)] min-h-[500px]">
      <div className="relative">
        <Search className="absolute left-2.5 top-2 text-[var(--qd-text-3)]" size={12} />
        <input
          type="text"
          placeholder="Search concepts or tags..."
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-[var(--qd-radius-sm)] pl-7 pr-3 py-1.5 text-[11px] text-[var(--qd-text)] placeholder-[var(--qd-text-3)] focus:outline-none focus:border-[var(--qd-accent)]"
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
                onClick={() => onToggleTopic(topic)}
                className="w-full flex items-center justify-between text-left text-[10px] font-bold font-head uppercase tracking-wider text-[var(--qd-text-2)] hover:text-[var(--qd-text)] py-1"
              >
                <div className="flex items-center gap-1.5 font-sans">
                  <FolderOpen size={12} className="text-[var(--qd-accent)]" />
                  <span>{topic}</span>
                  <span className="font-mono text-[10px] text-[var(--qd-text-3)] bg-[var(--qd-surface-2)] px-1 rounded font-bold">
                    {notesInTopic.length}
                  </span>
                  {topic === "Trading Rules" && auditResult?.status === "warning" && (
                    <AlertTriangle size={12} className="text-[var(--qd-warn)] animate-pulse" title="Configuration mismatches detected!" />
                  )}
                </div>
                {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              </button>

              {isExpanded && (
                <div className="mt-1 pl-3 space-y-1">
                  {notesInTopic.length === 0 ? (
                    <div className="text-[11px] text-[var(--qd-text-3)] italic py-1">No notes</div>
                  ) : (
                    notesInTopic.map((note) => {
                      const isSelected = selectedNote?.id === note.id;
                      return (
                        <button
                          key={note.id}
                          type="button"
                          onClick={() => onSelectNote(note.id)}
                          className={`w-full text-left text-[11px] py-1 px-2 rounded-[var(--qd-radius-sm)] flex flex-col gap-1 transition-colors ${
                            isSelected
                              ? "bg-[var(--qd-surface-3)] text-[var(--qd-text)] border border-[var(--qd-border-strong)]"
                              : "text-[var(--qd-text-2)] hover:bg-[var(--qd-surface-2)] hover:text-[var(--qd-text)] border border-transparent"
                          }`}
                        >
                          <div className="flex items-center gap-2 w-full">
                            <FileText size={12} className="shrink-0" />
                            <span className="truncate font-medium">{note.title}</span>
                          </div>
                          {note.snippet && (
                            <span className="text-[10px] text-[var(--qd-text-3)] font-sans line-clamp-2 pl-5 break-words">
                              {note.snippet}
                            </span>
                          )}
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
  );
}
