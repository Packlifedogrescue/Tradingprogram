"use client";

import { useEffect, useRef } from "react";

export function LiveMarketGraph({ symbol = "SP:SPX", interval = "5" }: { symbol?: string; interval?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    ref.current.innerHTML = "";

    const container = document.createElement("div");
    container.className = "tradingview-widget-container__widget h-full w-full";
    container.id = "tv-advanced-chart";
    ref.current.appendChild(container);

    const script = document.createElement("script");
    script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    script.type = "text/javascript";
    script.async = true;
    script.innerHTML = JSON.stringify({
      autosize: true,
      symbol,
      interval,
      timezone: "America/New_York",
      theme: "dark",
      style: "1",
      locale: "en",
      backgroundColor: "#05070b",
      gridColor: "rgba(255,255,255,0.06)",
      hide_top_toolbar: false,
      allow_symbol_change: true,
      save_image: false,
      withdateranges: false,
      container_id: "tv-advanced-chart",
    });

    ref.current.appendChild(script);
  }, [symbol, interval]);

  return <div ref={ref} className="h-[420px] w-full rounded-[24px] overflow-hidden border border-white/8 bg-black/20" />;
}
