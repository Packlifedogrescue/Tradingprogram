'use client';

import { useEffect, useId } from 'react';

type Props = {
  symbol?: string;
  interval?: string;
  theme?: 'dark' | 'light';
  height?: number;
};

export default function TradingViewEmbed({
  symbol = 'CME_MINI:NQ1!',
  interval = '5',
  theme = 'dark',
  height = 640,
}: Props) {
  const containerId = useId().replace(/:/g, '');

  useEffect(() => {
    const existing = document.getElementById(`tv-script-${containerId}`);
    if (existing) existing.remove();

    const script = document.createElement('script');
    script.id = `tv-script-${containerId}`;
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
    script.type = 'text/javascript';
    script.async = true;
    script.innerHTML = JSON.stringify({
      autosize: true,
      symbol,
      interval,
      timezone: 'America/New_York',
      theme,
      style: '1',
      locale: 'en',
      allow_symbol_change: true,
      calendar: false,
      support_host: 'https://www.tradingview.com',
    });

    const target = document.getElementById(containerId);
    if (target) {
      target.innerHTML = '';
      target.appendChild(script);
    }
  }, [containerId, interval, symbol, theme]);

  return (
    <div className="rounded-[1.5rem] border border-white/10 bg-black/40 p-3">
      <div className="mb-3 flex items-center justify-between px-2 text-xs uppercase tracking-[0.28em] text-white/45">
        <span>TradingView real widget</span>
        <span>Public embed</span>
      </div>
      <div className="tradingview-widget-container overflow-hidden rounded-[1rem] border border-white/8" style={{ height }}>
        <div id={containerId} className="h-full w-full" />
      </div>
    </div>
  );
}
