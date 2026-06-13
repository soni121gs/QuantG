import React from "react";

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
  let list = null;
  let para = [];
  const flushPara = () => { if (para.length) { blocks.push({ type: "p", content: para.join(" ") }); para = []; } };
  const flushList = () => { if (list) { blocks.push(list); list = null; } };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { flushPara(); flushList(); continue; }
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    const ul = /^[-*]\s+(.*)$/.exec(line);
    const ol = /^\d+\.\s+(.*)$/.exec(line);
    if (h) { flushPara(); flushList(); blocks.push({ type: "h", level: h[1].length, content: h[2] }); }
    else if (ul) { flushPara(); if (!list || list.type !== "ul") { flushList(); list = { type: "ul", items: [] }; } list.items.push(ul[1]); }
    else if (ol) { flushPara(); if (!list || list.type !== "ol") { flushList(); list = { type: "ol", items: [] }; } list.items.push(ol[1]); }
    else { flushList(); para.push(line); }
  }
  flushPara();
  flushList();
  return blocks.map((b, i) => {
    if (b.type === "h") {
      return <div key={i} className={`font-head font-bold text-[var(--qd-text)] mt-2 first:mt-0 ${b.level === 1 ? "text-base" : "text-sm"}`}>{renderInline(b.content)}</div>;
    }
    if (b.type === "ul") return <ul key={i} className="my-1.5 list-disc space-y-1 pl-5">{b.items.map((it, j) => <li key={j}>{renderInline(it)}</li>)}</ul>;
    if (b.type === "ol") return <ol key={i} className="my-1.5 list-decimal space-y-1 pl-5">{b.items.map((it, j) => <li key={j}>{renderInline(it)}</li>)}</ol>;
    return <p key={i} className="my-1.5 leading-relaxed first:mt-0">{renderInline(b.content)}</p>;
  });
};
