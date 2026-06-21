import React, { useState, useEffect, useRef } from "react";

export default function PhysicsGraphCanvas({ data, currentTitle, onSelectNode }) {
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
      <style dangerouslySetInnerHTML={{__html: `
        @keyframes linkFlow {
          to {
            stroke-dashoffset: -20;
          }
        }
        .flow-link {
          stroke-dasharray: 6, 4;
          animation: linkFlow 0.8s linear infinite;
        }
        .glow-node {
          transition: filter 0.2s ease, r 0.2s ease;
        }
        .glow-node:hover {
          filter: drop-shadow(0 0 6px var(--node-color));
          r: 7.5px !important;
        }
      `}} />
      <div className="text-[11px] font-semibold uppercase tracking-wider text-[var(--qd-text-3)] mb-2 flex items-center justify-between">
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
              strokeOpacity={isHighlighted ? 0.9 : 0.4}
              className={isHighlighted ? "flow-link" : ""}
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
          const nodeColor = getTopicColor(node.topic);
          
          return (
            <g
              key={node.id}
              className="transition-opacity duration-150"
              style={{ opacity: hoveredNode && hoveredNode !== node.id && !isLinked && !isCurrent ? 0.35 : 1 }}
              onMouseEnter={() => setHoveredNode(node.id)}
              onMouseLeave={() => setHoveredNode(null)}
              onClick={() => onSelectNode(node.id)}
            >
              <circle
                cx={node.x}
                cy={node.y}
                r={isCurrent ? 7.5 : 5}
                fill={nodeColor}
                stroke={isCurrent ? "var(--qd-text)" : "transparent"}
                strokeWidth={2}
                style={{ "--node-color": nodeColor }}
                className="cursor-pointer glow-node"
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
                  style={{ filter: "drop-shadow(0 0 4px var(--qd-accent))" }}
                />
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
