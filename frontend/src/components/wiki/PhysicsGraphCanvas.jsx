import React, { useState, useEffect, useRef, useCallback } from "react";
import { ZoomIn, ZoomOut, Maximize2 } from "lucide-react";

// Logical canvas size (the physics world). The SVG scales to its container via
// viewBox, so this stays fixed while the on-screen map grows with the column.
const BASE_W = 600;
const BASE_H = 520;
const MIN_W = BASE_W / 2.6; // most zoomed-in
const MAX_W = BASE_W * 1.7; // most zoomed-out

export default function PhysicsGraphCanvas({ data, currentTitle, onSelectNode }) {
  const canvasRef = useRef(null);
  const [nodes, setNodes] = useState([]);
  const [links, setLinks] = useState([]);
  const [hoveredNode, setHoveredNode] = useState(null);
  const dragNodeRef = useRef(null);
  const panRef = useRef(null); // {startX, startY, vbX, vbY} while panning empty space

  const center = { x: BASE_W / 2, y: BASE_H / 2 };
  // viewBox state drives zoom + pan
  const [vb, setVb] = useState({ x: 0, y: 0, w: BASE_W, h: BASE_H });

  // Map a screen-space pointer event to graph (viewBox) coordinates.
  const toGraph = useCallback((e) => {
    const rect = canvasRef.current.getBoundingClientRect();
    return {
      x: vb.x + (e.clientX - rect.left) * (vb.w / rect.width),
      y: vb.y + (e.clientY - rect.top) * (vb.h / rect.height),
    };
  }, [vb]);

  // Set up nodes and links when data changes
  useEffect(() => {
    if (!data || !data.nodes) return;
    const existingNodesMap = new Map(nodes.map((n) => [n.id, n]));
    const newNodes = data.nodes.map((node) => {
      const existing = existingNodesMap.get(node.id);
      return {
        id: node.id,
        topic: node.topic,
        x: existing ? existing.x : center.x + (Math.random() - 0.5) * 120,
        y: existing ? existing.y : center.y + (Math.random() - 0.5) * 120,
        vx: existing ? existing.vx : 0,
        vy: existing ? existing.vy : 0,
      };
    });
    const newLinks = data.links.map((link) => ({ source: link.source, target: link.target }));
    setNodes(newNodes);
    setLinks(newLinks);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, currentTitle]);

  // Spring physics loop
  useEffect(() => {
    if (nodes.length === 0) return;
    const REPULSION = 1600;
    const ATTRACTION = 0.04;
    const GRAVITY = 0.018;
    const DAMPING = 0.85;
    let animId;

    const step = () => {
      setNodes((prevNodes) => {
        const nextNodes = prevNodes.map((n) => ({ ...n }));
        const nodeMap = new Map(nextNodes.map((n) => [n.id, n]));

        for (let i = 0; i < nextNodes.length; i++) {
          const n1 = nextNodes[i];
          if (n1 === dragNodeRef.current) continue;
          for (let j = i + 1; j < nextNodes.length; j++) {
            const n2 = nextNodes[j];
            const dx = n2.x - n1.x || 0.1;
            const dy = n2.y - n1.y || 0.1;
            const distSq = dx * dx + dy * dy;
            const dist = Math.sqrt(distSq) || 1;
            if (dist < 240) {
              const force = REPULSION / (distSq + 20);
              const fx = (dx / dist) * force;
              const fy = (dy / dist) * force;
              n1.vx -= fx; n1.vy -= fy;
              n2.vx += fx; n2.vy += fy;
            }
          }
        }

        links.forEach((link) => {
          const sNode = nodeMap.get(link.source);
          const tNode = nodeMap.get(link.target);
          if (!sNode || !tNode) return;
          const dx = tNode.x - sNode.x;
          const dy = tNode.y - sNode.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const targetDist = 95;
          const force = (dist - targetDist) * ATTRACTION;
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          if (sNode !== dragNodeRef.current) { sNode.vx += fx; sNode.vy += fy; }
          if (tNode !== dragNodeRef.current) { tNode.vx -= fx; tNode.vy -= fy; }
        });

        nextNodes.forEach((n) => {
          if (n === dragNodeRef.current) return;
          n.vx += (center.x - n.x) * GRAVITY;
          n.vy += (center.y - n.y) * GRAVITY;
          n.vx *= DAMPING; n.vy *= DAMPING;
          n.x += n.vx; n.y += n.vy;
          n.x = Math.max(14, Math.min(BASE_W - 14, n.x));
          n.y = Math.max(14, Math.min(BASE_H - 14, n.y));
        });

        return nextNodes;
      });
      animId = requestAnimationFrame(step);
    };

    animId = requestAnimationFrame(step);
    return () => cancelAnimationFrame(animId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes.length, links]);

  // ---- Interaction: node drag, empty-space pan, wheel zoom ----
  const handleNodeMouseDown = (node, e) => {
    e.stopPropagation();
    e.preventDefault();
    dragNodeRef.current = node;
  };

  const handleCanvasMouseDown = (e) => {
    if (dragNodeRef.current) return;
    panRef.current = { startX: e.clientX, startY: e.clientY, vbX: vb.x, vbY: vb.y };
  };

  const handleMouseMove = (e) => {
    if (dragNodeRef.current && canvasRef.current) {
      const g = toGraph(e);
      setNodes((prev) =>
        prev.map((n) => {
          if (n.id === dragNodeRef.current.id) {
            n.x = g.x; n.y = g.y; n.vx = 0; n.vy = 0;
            dragNodeRef.current = n;
          }
          return n;
        })
      );
      return;
    }
    if (panRef.current && canvasRef.current) {
      const rect = canvasRef.current.getBoundingClientRect();
      const dx = (e.clientX - panRef.current.startX) * (vb.w / rect.width);
      const dy = (e.clientY - panRef.current.startY) * (vb.h / rect.height);
      setVb((v) => ({ ...v, x: panRef.current.vbX - dx, y: panRef.current.vbY - dy }));
    }
  };

  const endInteraction = () => {
    dragNodeRef.current = null;
    panRef.current = null;
  };

  const handleWheel = (e) => {
    e.preventDefault();
    const rect = canvasRef.current.getBoundingClientRect();
    const mx = vb.x + (e.clientX - rect.left) * (vb.w / rect.width);
    const my = vb.y + (e.clientY - rect.top) * (vb.h / rect.height);
    const factor = e.deltaY > 0 ? 1.12 : 0.89;
    setVb((v) => {
      let newW = Math.max(MIN_W, Math.min(MAX_W, v.w * factor));
      const ratio = newW / v.w;
      const newH = v.h * ratio;
      return {
        w: newW,
        h: newH,
        x: mx - (mx - v.x) * ratio,
        y: my - (my - v.y) * ratio,
      };
    });
  };

  const zoomBy = (factor) => setVb((v) => {
    const newW = Math.max(MIN_W, Math.min(MAX_W, v.w * factor));
    const ratio = newW / v.w;
    const cx = v.x + v.w / 2;
    const cy = v.y + v.h / 2;
    const newH = v.h * ratio;
    return { w: newW, h: newH, x: cx - newW / 2, y: cy - newH / 2 };
  });
  const resetView = () => setVb({ x: 0, y: 0, w: BASE_W, h: BASE_H });

  const getTopicColor = (topic) => {
    const map = {
      "YouTube transcripts": "var(--qd-accent)",
      "Meeting transcripts": "var(--qd-profit)",
      "Decisions": "var(--qd-warn)",
      "Projects": "var(--qd-loss)",
      "Trading Rules": "rgb(168, 85, 247)",
    };
    return map[topic] || "var(--qd-text-3)";
  };

  const showAllLabels = nodes.length <= 28;
  const labelFor = (id) => (id.length > 22 ? id.slice(0, 21) + "…" : id);

  return (
    <div className="qd-card p-3 bg-[var(--qd-surface)] border border-[var(--qd-border)] relative">
      <style dangerouslySetInnerHTML={{ __html: `
        @keyframes linkFlow { to { stroke-dashoffset: -20; } }
        .flow-link { stroke-dasharray: 6, 4; animation: linkFlow 0.8s linear infinite; }
        .glow-node { transition: filter 0.2s ease; }
        .glow-node:hover { filter: drop-shadow(0 0 7px var(--node-color)); }
      `}} />
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--qd-text-3)]">
          <span>Knowledge Map</span>
          {hoveredNode && <span className="truncate max-w-[150px] text-[var(--qd-accent)] normal-case">{hoveredNode}</span>}
        </div>
        <div className="flex items-center gap-1">
          <button onClick={() => zoomBy(0.82)} title="Zoom in" className="grid h-6 w-6 place-items-center rounded border border-[var(--qd-border)] text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)]">
            <ZoomIn size={13} />
          </button>
          <button onClick={() => zoomBy(1.22)} title="Zoom out" className="grid h-6 w-6 place-items-center rounded border border-[var(--qd-border)] text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)]">
            <ZoomOut size={13} />
          </button>
          <button onClick={resetView} title="Reset view" className="grid h-6 w-6 place-items-center rounded border border-[var(--qd-border)] text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)]">
            <Maximize2 size={12} />
          </button>
        </div>
      </div>
      <svg
        ref={canvasRef}
        width="100%"
        height={520}
        viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
        className="bg-[var(--qd-surface-2)] rounded-[var(--qd-radius-sm)] cursor-grab active:cursor-grabbing touch-none select-none"
        onMouseDown={handleCanvasMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={endInteraction}
        onMouseLeave={endInteraction}
        onWheel={handleWheel}
      >
        {links.map((link, i) => {
          const s = nodes.find((n) => n.id === link.source);
          const t = nodes.find((n) => n.id === link.target);
          if (!s || !t) return null;
          const isHighlighted = s.id === currentTitle || t.id === currentTitle;
          return (
            <line
              key={i}
              x1={s.x} y1={s.y} x2={t.x} y2={t.y}
              stroke={isHighlighted ? "var(--qd-accent)" : "var(--qd-border)"}
              strokeWidth={isHighlighted ? 2 : 1.2}
              strokeOpacity={isHighlighted ? 0.9 : 0.4}
              className={isHighlighted ? "flow-link" : ""}
            />
          );
        })}

        {nodes.map((node) => {
          const isCurrent = node.id === currentTitle;
          const isLinked = links.some(
            (l) =>
              (l.source === currentTitle && l.target === node.id) ||
              (l.target === currentTitle && l.source === node.id)
          );
          const dim = hoveredNode && hoveredNode !== node.id && !isLinked && !isCurrent;
          const nodeColor = getTopicColor(node.topic);
          const showLabel = showAllLabels || isCurrent || isLinked || hoveredNode === node.id;
          return (
            <g
              key={node.id}
              className="transition-opacity duration-150"
              style={{ opacity: dim ? 0.3 : 1 }}
              onMouseEnter={() => setHoveredNode(node.id)}
              onMouseLeave={() => setHoveredNode(null)}
              onClick={() => onSelectNode(node.id)}
            >
              {isCurrent && (
                <circle cx={node.x} cy={node.y} r={16} fill="none" stroke="var(--qd-accent)" strokeWidth={1.5}
                  className="animate-pulse" style={{ filter: "drop-shadow(0 0 5px var(--qd-accent))" }} />
              )}
              <circle
                cx={node.x} cy={node.y}
                r={isCurrent ? 9 : hoveredNode === node.id ? 8 : 6}
                fill={nodeColor}
                stroke={isCurrent ? "var(--qd-text)" : "transparent"}
                strokeWidth={2}
                style={{ "--node-color": nodeColor }}
                className="cursor-pointer glow-node"
                onMouseDown={(e) => handleNodeMouseDown(node, e)}
              />
              {showLabel && (
                <text
                  x={node.x}
                  y={node.y + (isCurrent ? 24 : 19)}
                  textAnchor="middle"
                  className="pointer-events-none select-none"
                  style={{ fontSize: 10, fontWeight: isCurrent ? 700 : 500, fill: isCurrent ? "var(--qd-text)" : "var(--qd-text-2)" }}
                >
                  {labelFor(node.id)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="mt-1.5 text-[10px] text-[var(--qd-text-3)]">Scroll to zoom · drag a node to move · drag canvas to pan</div>
    </div>
  );
}
