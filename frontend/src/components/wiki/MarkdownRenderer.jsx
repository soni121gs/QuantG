import React from "react";

export default function MarkdownRenderer({ content, onSelectNote }) {
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
            className="text-[var(--qd-accent)] hover:underline font-bold inline-block bg-transparent p-0 border-none cursor-pointer align-baseline font-sans text-sm"
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
              <code key={k} className="px-1.5 py-0.5 rounded bg-[var(--qd-surface-3)] font-mono text-[11px] text-[var(--qd-warn)]">
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
        <ul key={key} className="list-disc pl-4 my-1.5 space-y-1 text-[var(--qd-text-2)] text-xs">
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
          <pre key={`code-${index}`} className="p-2.5 my-2 rounded bg-[var(--qd-surface-2)] border border-[var(--qd-border)] overflow-x-auto font-mono text-[11px] text-[var(--qd-text-2)]">
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
        <h1 key={index} className="text-[17px] font-extrabold font-head text-[var(--qd-text)] mt-4 mb-2 border-b border-[var(--qd-border)] pb-1">
          {parseInline(line.substring(2))}
        </h1>
      );
    } else if (line.startsWith("## ")) {
      flushList(`list-before-h2-${index}`);
      elements.push(
        <h2 key={index} className="text-[14px] font-bold font-head text-[var(--qd-text)] mt-3 mb-1.5">
          {parseInline(line.substring(3))}
        </h2>
      );
    } else if (line.startsWith("### ")) {
      flushList(`list-before-h3-${index}`);
      elements.push(
        <h3 key={index} className="text-[12px] font-semibold font-head text-[var(--qd-text)] mt-2.5 mb-1">
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
      elements.push(<hr key={index} className="my-4 border-[var(--qd-border)]" />);
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
        <p key={index} className="leading-relaxed text-xs text-[var(--qd-text-2)] my-1">
          {parseInline(line)}
        </p>
      );
    }
  });

  flushList("list-final");
  return <div className="space-y-1">{elements}</div>;
}
