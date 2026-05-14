import React, { useEffect, useState } from "react";
import Marquee from "react-fast-marquee";
import { api, formatINR, pctFmt } from "../lib/api";
import { TrendingUp, TrendingDown } from "lucide-react";

export default function TickerTape() {
  const [data, setData] = useState([]);

  useEffect(() => {
    let alive = true;
    const fetch = () =>
      api
        .get("/market/watchlist")
        .then((r) => alive && setData(r.data))
        .catch(() => {});
    fetch();
    const t = setInterval(fetch, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  if (!data.length) return null;

  return (
    <div
      className="border-y border-[var(--qd-border)] bg-[var(--qd-surface)] py-2"
      data-testid="ticker-tape"
    >
      <Marquee gradient={false} speed={42} pauseOnHover>
        {data.map((s, i) => {
          const up = s.change >= 0;
          return (
            <div
              key={i}
              className="flex items-center gap-2 px-6 border-r border-[var(--qd-border)]"
              data-testid={`ticker-${s.symbol}`}
            >
              <span className="font-mono text-xs text-[var(--qd-text-2)]">{s.symbol}</span>
              <span className="font-mono text-sm text-white">{formatINR(s.price)}</span>
              <span
                className={`font-mono text-xs flex items-center gap-1 ${
                  up ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"
                }`}
              >
                {up ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                {pctFmt(s.pct)}
              </span>
            </div>
          );
        })}
      </Marquee>
    </div>
  );
}
