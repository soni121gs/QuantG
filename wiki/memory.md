# QuantG Auto-Memory Ledger

This file is a persistent memory ledger. AI agents (Claude, Codex, Gemini) are required to read this file at the start of a session and append their session summary and learnings here at the end of every session.

## Session Logs

| Date | Agent | Session Summary & Core Decisions | Key Technical Challenges & Solutions |
|---|---|---|---|
| 2026-06-18 | Codex | Stage 1A Event Bus Redesign & route extraction completed. | Extracted remaining API routes out of `server.py` to modularize code. |
| 2026-06-19 | Antigravity (Gemini 3.5 Flash) | Implemented the Wikis, Backlinks, and Auto-Memory Knowledge Hub system. Created folder structure, added routing rules to CLAUDE.md/AGENTS.md, created REST endpoints in routes/wiki.py, and built the Wiki.jsx page with physics-simulated SVG link graph. | Resolved Windows Temp directory access permission errors in backend tests by implementing tempfile.TemporaryDirectory with a local scratch path fallback. Resolved React Hooks warnings in visual graph SVG. |

