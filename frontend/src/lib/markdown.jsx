import React, { useState } from "react";
import { Copy, Check } from "lucide-react";

// CodeBlock component to render blocks with copy capability
const CodeBlock = ({ code, language }) => {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <div className="relative my-3 rounded-lg border border-[var(--qd-border)] bg-[#0d1117] text-[#c9d1d9] overflow-hidden shadow-inner font-mono text-[13px] leading-relaxed">
      <div className="flex items-center justify-between px-4 py-2 border-b border-[#21262d] bg-[#161b22] text-[#8b949e] text-xs select-none">
        <span className="uppercase font-semibold tracking-wider">{language || "code"}</span>
        <button 
          onClick={handleCopy} 
          className="hover:text-white transition-colors flex items-center gap-1 font-sans font-medium active:scale-95 text-xs text-[#8b949e] cursor-pointer"
          type="button"
        >
          {copied ? (
            <>
              <Check size={12} className="text-emerald-400" />
              <span className="text-emerald-400">Copied!</span>
            </>
          ) : (
            <>
              <Copy size={12} />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      <pre className="p-4 overflow-x-auto whitespace-pre font-mono">
        <code>{code}</code>
      </pre>
    </div>
  );
};

// Minimal, dependency-free markdown renderer (bold, inline code, bullet/numbered
// lists, headings). Builds real React nodes — no dangerouslySetInnerHTML — so
// Gemini's markdown stops showing as raw ** **. Shared by the agent chat and the
// Market Hub brief.
export const renderInline = (text) => {
  const parts = [];
  const regex = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let lastIndex = 0;
  let match;
  let key = 0;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index));
    const token = match[0];
    if (token.startsWith("**")) {
      parts.push(<strong key={key++} className="font-semibold text-[var(--qd-text)]">{token.slice(2, -2)}</strong>);
    } else {
      parts.push(<code key={key++} className="rounded bg-[var(--qd-bg)] px-1 py-0.5 font-mono text-[0.85em] text-[var(--qd-accent)]">{token.slice(1, -1)}</code>);
    }
    lastIndex = regex.lastIndex;
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts;
};

export const renderMarkdown = (text) => {
  const lines = String(text || "").split("\n");
  const blocks = [];
  
  let inCodeBlock = false;
  let codeLines = [];
  let codeLang = "";
  
  let inTable = false;
  let tableHeader = null;
  let tableRows = [];
  
  let list = null;
  let para = [];
  
  const flushPara = () => {
    if (para.length) {
      blocks.push({ type: "p", content: para.join(" ") });
      para = [];
    }
  };
  
  const flushList = () => {
    if (list) {
      blocks.push(list);
      list = null;
    }
  };
  
  const flushTable = () => {
    if (inTable && (tableHeader || tableRows.length)) {
      blocks.push({ type: "table", header: tableHeader, rows: tableRows });
      tableHeader = null;
      tableRows = [];
      inTable = false;
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const rawLine = lines[i];
    const trimmed = rawLine.trim();
    
    // Code block handling
    if (trimmed.startsWith("```")) {
      if (inCodeBlock) {
        // End of code block
        blocks.push({ type: "code", language: codeLang, content: codeLines.join("\n") });
        codeLines = [];
        codeLang = "";
        inCodeBlock = false;
      } else {
        // Start of code block
        flushPara();
        flushList();
        flushTable();
        inCodeBlock = true;
        codeLang = trimmed.slice(3).trim();
      }
      continue;
    }
    
    if (inCodeBlock) {
      codeLines.push(rawLine);
      continue;
    }
    
    // Table detection: starts and ends with '|' and contains cell divisions
    const isTableLine = trimmed.startsWith("|") && trimmed.endsWith("|");
    if (isTableLine) {
      flushPara();
      flushList();
      
      const cells = trimmed.split("|").map(c => c.trim()).slice(1, -1);
      
      // Check if this is a separator line (e.g. |---| or | :--- |)
      const isSeparator = cells.every(c => /^[:-]+$/.test(c));
      
      if (isSeparator) {
        // Just a separator, ensure we are in a table state and skip
        inTable = true;
        continue;
      }
      
      if (!inTable) {
        // This is the first row, treat as header
        tableHeader = cells;
        inTable = true;
      } else {
        // Regular row
        tableRows.push(cells);
      }
      continue;
    } else {
      if (inTable) {
        flushTable();
      }
    }
    
    // Blank line flushes lists and paragraphs
    if (!trimmed) {
      flushPara();
      flushList();
      continue;
    }
    
    // Headings
    const hMatch = /^(#{1,6})\s+(.*)$/.exec(trimmed);
    if (hMatch) {
      flushPara();
      flushList();
      blocks.push({ type: "h", level: hMatch[1].length, content: hMatch[2] });
      continue;
    }
    
    // Unordered lists
    const ulMatch = /^[-*+]\s+(.*)$/.exec(trimmed);
    if (ulMatch) {
      flushPara();
      if (!list || list.type !== "ul") {
        flushList();
        list = { type: "ul", items: [] };
      }
      list.items.push(ulMatch[1]);
      continue;
    }
    
    // Ordered lists
    const olMatch = /^(\d+)\.\s+(.*)$/.exec(trimmed);
    if (olMatch) {
      flushPara();
      if (!list || list.type !== "ol") {
        flushList();
        list = { type: "ol", items: [] };
      }
      list.items.push(olMatch[2]);
      continue;
    }
    
    // Normal paragraph line
    flushList();
    para.push(trimmed);
  }
  
  // Flush remaining state
  flushPara();
  flushList();
  flushTable();
  if (inCodeBlock && codeLines.length) {
    blocks.push({ type: "code", language: codeLang, content: codeLines.join("\n") });
  }

  return blocks.map((b, i) => {
    if (b.type === "h") {
      return (
        <div key={i} className={`font-head font-bold text-[var(--qd-text)] mt-4 mb-2 first:mt-0 ${b.level === 1 ? "text-lg border-b border-[var(--qd-border)] pb-1" : b.level === 2 ? "text-base" : "text-sm"}`}>
          {renderInline(b.content)}
        </div>
      );
    }
    if (b.type === "ul") {
      return (
        <ul key={i} className="my-2 list-disc space-y-1 pl-5 text-[var(--qd-text)]">
          {b.items.map((it, j) => (
            <li key={j} className="leading-relaxed">{renderInline(it)}</li>
          ))}
        </ul>
      );
    }
    if (b.type === "ol") {
      return (
        <ol key={i} className="my-2 list-decimal space-y-1 pl-5 text-[var(--qd-text)]">
          {b.items.map((it, j) => (
            <li key={j} className="leading-relaxed">{renderInline(it)}</li>
          ))}
        </ol>
      );
    }
    if (b.type === "code") {
      return <CodeBlock key={i} code={b.content} language={b.language} />;
    }
    if (b.type === "table") {
      return (
        <div key={i} className="my-3 overflow-x-auto rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)]/30 max-w-full">
          <table className="min-w-full divide-y divide-[var(--qd-border)] font-sans text-xs">
            {b.header && (
              <thead className="bg-[var(--qd-surface-3)]/60 text-[var(--qd-text-2)] font-semibold uppercase tracking-wider animate-fade-in">
                <tr>
                  {b.header.map((col, idx) => (
                    <th key={idx} className="px-3 py-2.5 text-left">{renderInline(col)}</th>
                  ))}
                </tr>
              </thead>
            )}
            <tbody className="divide-y divide-[var(--qd-border)]/50 text-[var(--qd-text)]">
              {b.rows.map((row, rowIdx) => (
                <tr key={rowIdx} className="hover:bg-[var(--qd-surface-2)]/50 transition-colors odd:bg-[var(--qd-surface-2)]/10">
                  {row.map((cell, cellIdx) => (
                    <td key={cellIdx} className="px-3 py-2 whitespace-nowrap">{renderInline(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    return (
      <p key={i} className="my-2 leading-relaxed first:mt-0 text-[var(--qd-text)]">
        {renderInline(b.content)}
      </p>
    );
  });
};
