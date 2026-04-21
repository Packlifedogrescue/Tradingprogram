"use client";

import { useEffect, useState } from "react";

type Candle = {
  o: number;
  c: number;
  h: number;
  l: number;
};

export default function Tradovation() {
  const [candles, setCandles] = useState<Candle[]>([]);

  // fake live data
  useEffect(() => {
    let last = 100;

    const interval = setInterval(() => {
      const open = last;
      const close = open + (Math.random() - 0.5) * 2;
      const high = Math.max(open, close) + Math.random();
      const low = Math.min(open, close) - Math.random();

      last = close;

      setCandles((prev) => {
        const next = [...prev, { o: open, c: close, h: high, l: low }];
        return next.slice(-50);
      });
    }, 500);

    return () => clearInterval(interval);
  }, []);

  return (
    <div className="p-6 bg-black text-white min-h-screen">
      <h1 className="text-xl mb-4">Live Chart (Simulated)</h1>

      <div className="flex items-end gap-[2px] h-[300px] border border-white/20 p-2">
        {candles.map((candle, i) => {
          const isUp = candle.c > candle.o;

          return (
            <div key={i} className="flex flex-col items-center w-[6px]">
              {/* wick */}
              <div
                className="bg-white/40 w-[2px]"
                style={{
                  height: `${(candle.h - candle.l) * 20}px`,
                }}
              />

              {/* body */}
              <div
                className={isUp ? "bg-green-400 w-full" : "bg-red-400 w-full"}
                style={{
                  height: `${Math.abs(candle.c - candle.o) * 20}px`,
                }}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}
