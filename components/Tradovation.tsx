'use client';

import { motion } from 'framer-motion';
import { useEffect, useMemo, useRef, useState } from 'react';
import TradingViewEmbed from './TradingViewEmbed';

type Candle = { o: number; c: number; h: number; l: number; t: string };
type Drawing =
  | { type: 'hline'; y: number }
  | { type: 'trend'; start: { x: number; y: number }; end: { x: number; y: number } }
  | { type: 'range'; start: { x: number; y: number }; end: { x: number; y: number } };

type Trade = { side: 'BUY' | 'SELL'; symbol: string; price: string; time: string };

type SymbolProfile = {
  base: number;
  vol: number;
  drift: number;
  spread: number;
  pct: string;
  venue: string;
};

const symbolProfiles: Record<string, SymbolProfile> = {
  NQ: { base: 26743.5, vol: 14, drift: 0.18, spread: 0.25, pct: '+0.41%', venue: 'CME' },
  ES: { base: 5432.25, vol: 8, drift: 0.12, spread: 0.25, pct: '+0.26%', venue: 'CME' },
  YM: { base: 40185, vol: 28, drift: 0.15, spread: 1, pct: '+0.19%', venue: 'CBOT' },
  RTY: { base: 2218.8, vol: 7, drift: 0.11, spread: 0.1, pct: '+0.33%', venue: 'CME' },
  MNQ: { base: 26743.5, vol: 14, drift: 0.18, spread: 0.25, pct: '+0.39%', venue: 'CME' },
  MES: { base: 5432.25, vol: 8, drift: 0.12, spread: 0.25, pct: '+0.24%', venue: 'CME' },
  MYM: { base: 40185, vol: 28, drift: 0.15, spread: 1, pct: '+0.17%', venue: 'CBOT' },
  M2K: { base: 2218.8, vol: 7, drift: 0.11, spread: 0.1, pct: '+0.30%', venue: 'CME' },
  CL: { base: 78.42, vol: 1.6, drift: 0.03, spread: 0.02, pct: '+1.10%', venue: 'NYMEX' },
  MCL: { base: 78.42, vol: 1.6, drift: 0.03, spread: 0.02, pct: '+1.04%', venue: 'NYMEX' },
  GC: { base: 2388.4, vol: 11, drift: 0.05, spread: 0.1, pct: '+0.65%', venue: 'COMEX' },
  MGC: { base: 2388.4, vol: 11, drift: 0.05, spread: 0.1, pct: '+0.60%', venue: 'COMEX' },
  SI: { base: 31.18, vol: 0.22, drift: 0.01, spread: 0.01, pct: '+0.48%', venue: 'COMEX' },
  NG: { base: 2.64, vol: 0.08, drift: 0.004, spread: 0.001, pct: '+1.32%', venue: 'NYMEX' },
  ZB: { base: 118.53, vol: 0.45, drift: 0.01, spread: 0.01, pct: '+0.22%', venue: 'CBOT' },
  ZN: { base: 110.84, vol: 0.18, drift: 0.008, spread: 0.01, pct: '+0.18%', venue: 'CBOT' },
  '6E': { base: 1.0842, vol: 0.004, drift: 0.0004, spread: 0.0001, pct: '+0.35%', venue: 'CME' },
  '6J': { base: 0.00658, vol: 0.00004, drift: 0.000003, spread: 0.000001, pct: '+0.27%', venue: 'CME' },
  '6B': { base: 1.2764, vol: 0.004, drift: 0.0003, spread: 0.0001, pct: '+0.31%', venue: 'CME' },
  BTC: { base: 68420, vol: 180, drift: 2.4, spread: 1, pct: '+0.82%', venue: 'CRYPTO' },
};

const watchlist = [
  { group: 'Index Futures', items: ['NQ', 'ES', 'YM', 'RTY'] },
  { group: 'Micro Index', items: ['MNQ', 'MES', 'MYM', 'M2K'] },
  { group: 'Energy & Metals', items: ['CL', 'MCL', 'GC', 'MGC', 'SI', 'NG'] },
  { group: 'Rates & FX', items: ['ZB', 'ZN', '6E', '6J', '6B'] },
];

function makeSeries(base: number, vol: number, drift: number): Candle[] {
  const labels = ['09:35', '09:40', '09:45', '09:50', '09:55', '10:00', '10:05', '10:10', '10:15', '10:20', '10:25', '10:30', '10:35', '10:40', '10:45', '10:50'];
  let last = base - vol * 0.8;
  return labels.map((t, i) => {
    const directional = Math.sin(i * 0.8) * vol * 0.35 + drift * i;
    const o = last;
    const c = Number((base + directional + (i % 2 === 0 ? vol * 0.18 : -vol * 0.1)).toFixed(4));
    const h = Number((Math.max(o, c) + vol * (0.16 + (i % 3) * 0.05)).toFixed(4));
    const l = Number((Math.min(o, c) - vol * (0.14 + (i % 4) * 0.04)).toFixed(4));
    last = c;
    return { o: Number(o.toFixed(4)), c, h, l, t };
  });
}

export default function Tradovation() {
  const [symbol, setSymbol] = useState('NQ');
  const [timeframe, setTimeframe] = useState('5m');
  const [hoveredCandle, setHoveredCandle] = useState<number | null>(null);
  const [tool, setTool] = useState<'crosshair' | 'trend' | 'hline' | 'range'>('crosshair');
  const [draftPoint, setDraftPoint] = useState<{ x: number; y: number } | null>(null);
  const [drawings, setDrawings] = useState<Drawing[]>([]);
  const [selectedDrawing, setSelectedDrawing] = useState<number | string | null>(null);
  const [dragMode, setDragMode] = useState<string | null>(null);
  const [orderMode, setOrderMode] = useState(true);
  const [orderLevels, setOrderLevels] = useState({ entry: 62, stop: 48, target: 78 });
  const [liveTick, setLiveTick] = useState(0);
  const [executionFlash, setExecutionFlash] = useState<string | null>(null);
  const [tradeLog, setTradeLog] = useState<Trade[]>([]);
  const [hotkeyToast, setHotkeyToast] = useState<string | null>(null);
  const [showLevels, setShowLevels] = useState(true);
  const [sessionMode, setSessionMode] = useState<'RTH' | 'ETH'>('RTH');
  const [enabledIndicators, setEnabledIndicators] = useState(['EMA 20', 'VWAP']);
  const [showIndicatorMenu, setShowIndicatorMenu] = useState(false);
  const [useTradingView, setUseTradingView] = useState(false);
  const liveCandlesRef = useRef<Candle[]>([]);
  const profile = symbolProfiles[symbol] ?? symbolProfiles.NQ;

  const baseData = useMemo(() => {
    const out: Record<string, Candle[]> = {};
    Object.entries(symbolProfiles).forEach(([key, p]) => {
      out[key] = makeSeries(p.base, p.vol, p.drift);
    });
    return out;
  }, []);

  const baseCandles = useMemo(() => {
    const data = baseData[symbol] ?? baseData.NQ;
    const sizeMap: Record<string, number> = { '1m': 1, '2m': 1, '3m': 1, '5m': 1, '15m': 2, '30m': 3, '1h': 4, '4h': 6, '1d': 8, '1w': 8, '1mo': 8 };
    const group = sizeMap[timeframe] ?? 1;
    const grouped: Candle[] = [];
    for (let i = 0; i < data.length; i += group) {
      const chunk = data.slice(i, i + group);
      if (!chunk.length) continue;
      grouped.push({
        o: chunk[0].o,
        c: chunk[chunk.length - 1].c,
        h: Math.max(...chunk.map((x) => x.h)),
        l: Math.min(...chunk.map((x) => x.l)),
        t: chunk[chunk.length - 1].t,
      });
    }
    return grouped;
  }, [baseData, symbol, timeframe]);

  const [liveCandles, setLiveCandles] = useState<Candle[]>(baseCandles);

  useEffect(() => {
    setLiveCandles(baseCandles);
    liveCandlesRef.current = baseCandles;
  }, [baseCandles]);

  const candles = liveCandles;
  const minL = Math.min(...candles.map((c) => c.l));
  const maxH = Math.max(...candles.map((c) => c.h));
  const range = Math.max(maxH - minL, 0.0001);
  const toPct = (v: number) => ((v - minL) / range) * 76 + 8;
  const fromScreenPct = (yPct: number) => maxH - ((yPct - 8) / 76) * range;
  const active = hoveredCandle !== null ? candles[hoveredCandle] : null;
  const lastClose = candles[candles.length - 1]?.c ?? profile.base;
  const simulatedPrice = Number((lastClose + liveTick).toFixed(4));
  const simulatedPricePct = toPct(simulatedPrice);
  const entryPrice = fromScreenPct(orderLevels.entry);
  const unrealizedPnL = ((simulatedPrice - entryPrice) * 3).toFixed(2);
  const priceScale = Array.from({ length: 9 }, (_, i) => Number((maxH - (range / 8) * i).toFixed(4)));
  const bottomLabels = [candles[0]?.t, candles[Math.floor(candles.length * 0.25)]?.t, candles[Math.floor(candles.length * 0.5)]?.t, candles[Math.floor(candles.length * 0.75)]?.t, candles[candles.length - 1]?.t].filter(Boolean) as string[];
  const hoveredPrice = active ? active.c : simulatedPrice;
  const hoveredPricePct = active ? toPct(active.c) : simulatedPricePct;
  const hoveredTime = active?.t || candles[candles.length - 1]?.t || '10:30';
  const bid = Number((simulatedPrice - profile.spread).toFixed(4));
  const ask = Number((simulatedPrice + profile.spread).toFixed(4));

  useEffect(() => {
    const id = setInterval(() => {
      setLiveCandles((prev) => {
        if (!prev.length) return prev;
        const next = [...prev];
        const lastIndex = next.length - 1;
        const current = { ...next[lastIndex] };
        const step = (Math.random() - 0.5) * profile.vol * 0.06 + profile.drift * 0.01;
        const nextClose = Number((current.c + step).toFixed(4));
        current.c = nextClose;
        current.h = Number(Math.max(current.h, nextClose).toFixed(4));
        current.l = Number(Math.min(current.l, nextClose).toFixed(4));
        next[lastIndex] = current;
        if (Math.random() > 0.72) {
          const labelSeed = Date.now().toString().slice(-4);
          const newOpen = current.c;
          const newClose = Number((newOpen + step * 0.35).toFixed(4));
          next.push({
            o: Number(newOpen.toFixed(4)),
            c: newClose,
            h: Number((Math.max(newOpen, newClose) + profile.vol * 0.04).toFixed(4)),
            l: Number((Math.min(newOpen, newClose) - profile.vol * 0.04).toFixed(4)),
            t: labelSeed,
          });
          if (next.length > 28) next.shift();
        }
        liveCandlesRef.current = next;
        setLiveTick(Number((next[next.length - 1].c - (baseCandles[baseCandles.length - 1]?.c ?? profile.base)).toFixed(4)));
        return next;
      });
    }, 900);
    return () => clearInterval(id);
  }, [symbol, timeframe, profile.base, profile.drift, profile.vol, baseCandles]);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      const key = e.key.toLowerCase();
      if (['t', 'h', 'r', 'escape'].includes(key)) e.preventDefault();
      if (key === 't') {
        setTool('trend');
        setHotkeyToast('Trend tool');
      }
      if (key === 'h') {
        setTool('hline');
        setHotkeyToast('Horizontal line');
      }
      if (key === 'r') {
        setTool('range');
        setHotkeyToast('Range tool');
      }
      if (key === 'escape') {
        setTool('crosshair');
        setDraftPoint(null);
        setSelectedDrawing(null);
        setDragMode(null);
        setHotkeyToast('Crosshair');
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, []);

  useEffect(() => {
    if (!hotkeyToast) return;
    const id = setTimeout(() => setHotkeyToast(null), 1200);
    return () => clearTimeout(id);
  }, [hotkeyToast]);

  const toggleIndicator = (name: string) => {
    setEnabledIndicators((prev) => (prev.includes(name) ? prev.filter((x) => x !== name) : [...prev, name]));
  };

  const ema20Points = useMemo(() => {
    const k = 2 / 21;
    let ema = candles[0]?.c ?? 0;
    return candles.map((c) => {
      ema = c.c * k + ema * (1 - k);
      return ema;
    });
  }, [candles]);

  const vwapPoints = useMemo(() => {
    let cumulativePV = 0;
    let cumulativeV = 0;
    return candles.map((c, idx) => {
      const typical = (c.h + c.l + c.c) / 3;
      const vol = 100 + idx * 8;
      cumulativePV += typical * vol;
      cumulativeV += vol;
      return cumulativePV / cumulativeV;
    });
  }, [candles]);

  const normalizePoint = (clientX: number, clientY: number, rect: DOMRect) => ({
    x: Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100)),
    y: Math.max(8, Math.min(84, ((clientY - rect.top) / rect.height) * 100)),
  });

  const snapToCandle = (p: { x: number; y: number }) => {
    let closest: { x: number; y: number } | null = null;
    let minDist = 999;
    candles.forEach((c, i) => {
      const x = ((i + 0.5) / candles.length) * 100;
      const highY = 100 - toPct(c.h);
      const lowY = 100 - toPct(c.l);
      const dHigh = Math.hypot(x - p.x, highY - p.y);
      const dLow = Math.hypot(x - p.x, lowY - p.y);
      if (dHigh < minDist) {
        minDist = dHigh;
        closest = { x, y: highY };
      }
      if (dLow < minDist) {
        minDist = dLow;
        closest = { x, y: lowY };
      }
    });
    return minDist < 6 && closest ? closest : p;
  };

  const handleExecution = (side: 'BUY' | 'SELL') => {
    const price = simulatedPrice.toFixed(4);
    setExecutionFlash(`${side} order filled • ${symbol} @ ${price}`);
    setTradeLog((prev) => [
      {
        side,
        symbol,
        price,
        time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
      },
      ...prev,
    ].slice(0, 8));
    setTimeout(() => setExecutionFlash(null), 1600);
  };

  const tvSymbolMap: Record<string, string> = {
    NQ: 'CME_MINI:NQ1!',
    ES: 'CME_MINI:ES1!',
    YM: 'CBOT_MINI:YM1!',
    RTY: 'CME_MINI:RTY1!',
    MNQ: 'CME_MINI:NQ1!',
    MES: 'CME_MINI:ES1!',
    MYM: 'CBOT_MINI:YM1!',
    M2K: 'CME_MINI:RTY1!',
    CL: 'NYMEX:CL1!',
    MCL: 'NYMEX:CL1!',
    GC: 'COMEX:GC1!',
    MGC: 'COMEX:GC1!',
    SI: 'COMEX:SI1!',
    NG: 'NYMEX:NG1!',
    ZB: 'CBOT:ZB1!',
    ZN: 'CBOT:ZN1!',
    '6E': 'CME:6E1!',
    '6J': 'CME:6J1!',
    '6B': 'CME:6B1!',
    BTC: 'BITSTAMP:BTCUSD',
  };

  return (
    <div className="relative mx-auto max-w-7xl px-6 py-12 lg:px-8">
      <div className="mb-8 flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="text-sm font-semibold uppercase tracking-[0.35em] text-amber-300">Tradovation</div>
          <h1 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">Execution workspace + real charting flow</h1>
          <p className="mt-4 max-w-3xl text-lg leading-8 text-white/62">
            Simulated live candles, order-on-chart, indicators, watchlist, and an optional real TradingView widget layer.
          </p>
        </div>
        <button
          onClick={() => setUseTradingView((v) => !v)}
          className={`rounded-full border px-4 py-3 text-sm font-semibold ${useTradingView ? 'border-amber-400/25 bg-amber-400/10 text-amber-300' : 'border-white/10 bg-white/[0.03] text-white/75'}`}
        >
          {useTradingView ? 'Show custom chart' : 'Show TradingView widget'}
        </button>
      </div>

      {useTradingView ? (
        <div className="space-y-6">
          <TradingViewEmbed symbol={tvSymbolMap[symbol] ?? 'CME_MINI:NQ1!'} interval={timeframe === '1h' ? '60' : timeframe === '4h' ? '240' : timeframe === '1d' ? '1D' : timeframe === '1w' ? '1W' : timeframe === '1mo' ? '1M' : '5'} />
          <div className="rounded-[1.5rem] border border-white/10 bg-black/30 p-5 text-sm leading-7 text-white/62">
            This uses TradingView&apos;s public Advanced Chart widget. For a deeper private/product integration, swap this widget for TradingView Advanced Charts with your own datafeed and keep the attribution/public-use requirements in mind.
          </div>
        </div>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[1.12fr_0.88fr]">
          <motion.div className="overflow-hidden rounded-[2rem] border border-white/12 bg-black/32 shadow-[0_30px_100px_rgba(245,158,11,0.10)]">
            <div className="border-b border-white/10 px-5 py-4 sm:px-6">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <div className="text-[11px] uppercase tracking-[0.34em] text-amber-300">Tradovation chart</div>
                  <div className="mt-1 text-2xl font-semibold text-white">Execution workspace</div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <select value={symbol} onChange={(e) => setSymbol(e.target.value)} className="rounded-full border border-white/10 bg-black/40 px-3 py-1.5 text-xs text-white">
                    <optgroup label="Index Futures"><option value="NQ">NQ</option><option value="ES">ES</option><option value="YM">YM</option><option value="RTY">RTY</option></optgroup>
                    <optgroup label="Micro Index"><option value="MNQ">MNQ</option><option value="MES">MES</option><option value="MYM">MYM</option><option value="M2K">M2K</option></optgroup>
                    <optgroup label="Energy & Metals"><option value="CL">CL</option><option value="MCL">MCL</option><option value="GC">GC</option><option value="MGC">MGC</option><option value="SI">SI</option><option value="NG">NG</option></optgroup>
                    <optgroup label="Rates & FX"><option value="ZB">ZB</option><option value="ZN">ZN</option><option value="6E">6E</option><option value="6J">6J</option><option value="6B">6B</option></optgroup>
                    <optgroup label="Crypto"><option value="BTC">BTC</option></optgroup>
                  </select>
                  <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="rounded-full border border-white/10 bg-black/40 px-3 py-1.5 text-xs text-white">
                    <option value="1m">1M</option><option value="2m">2M</option><option value="3m">3M</option><option value="5m">5M</option><option value="15m">15M</option><option value="30m">30M</option><option value="1h">1H</option><option value="4h">4H</option><option value="1d">1D</option><option value="1w">1W</option><option value="1mo">1MO</option>
                  </select>
                </div>
              </div>
            </div>

            <div className="grid gap-4 p-4 xl:grid-cols-[1.18fr_0.82fr]">
              <div className="space-y-4">
                <div className="flex flex-wrap items-center gap-2 rounded-[1.2rem] border border-white/10 bg-black/28 px-4 py-3 text-[11px] text-white/65">
                  <div className="relative">
                    <button onClick={() => setShowIndicatorMenu((v) => !v)} className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 hover:text-white">Indicators</button>
                    {showIndicatorMenu && (
                      <div className="absolute left-0 top-10 z-30 w-40 rounded-xl border border-white/10 bg-black/95 p-2 shadow-[0_12px_40px_rgba(0,0,0,0.45)]">
                        {['EMA 20', 'VWAP', 'Volume'].map((name) => (
                          <button key={name} onClick={() => toggleIndicator(name)} className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-xs text-white/75 hover:bg-white/[0.04] hover:text-white">
                            <span>{name}</span>
                            <span>{enabledIndicators.includes(name) ? '✓' : ''}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  <button className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 hover:text-white">Alert</button>
                  <button className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 hover:text-white">Replay</button>
                  <button className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 hover:text-white">Layout</button>
                  <button onClick={() => { setDrawings([]); setDraftPoint(null); setOrderLevels({ entry: 62, stop: 48, target: 78 }); }} className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 hover:text-white">Reset chart</button>
                  <div className="ml-auto flex items-center gap-2">
                    <button onClick={() => setSessionMode((m) => (m === 'RTH' ? 'ETH' : 'RTH'))} className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-white/75">{sessionMode}</button>
                    <button onClick={() => setShowLevels((v) => !v)} className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-white/75">{showLevels ? 'Hide levels' : 'Show levels'}</button>
                  </div>
                </div>

                <div className="rounded-[1.4rem] border border-white/10 bg-[#0d0d0d] p-4">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <div>
                      <div className="text-[11px] uppercase tracking-[0.28em] text-white/38">{symbol} • {profile.venue}</div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-white/58">
                        <span>{timeframe.toUpperCase()}</span>
                        <span className="rounded-full border border-white/10 bg-white/[0.03] px-2 py-0.5 text-[10px]">{sessionMode}</span>
                        <span className="rounded-full border border-white/10 bg-white/[0.03] px-2 py-0.5 text-[10px]">Simulated</span>
                        <span className="rounded-full border border-white/10 bg-white/[0.03] px-2 py-0.5 text-[10px]">Bid {bid.toFixed(bid > 100 ? 2 : 4)}</span>
                        <span className="rounded-full border border-white/10 bg-white/[0.03] px-2 py-0.5 text-[10px]">Ask {ask.toFixed(ask > 100 ? 2 : 4)}</span>
                      </div>
                    </div>
                    <div className="hidden items-center gap-1 rounded-full border border-white/10 bg-black/40 p-1 sm:flex">
                      {(['crosshair', 'trend', 'hline', 'range'] as const).map((key) => (
                        <button key={key} onClick={() => { setTool(key); setDraftPoint(null); }} className={`rounded-full px-3 py-1.5 text-[11px] ${tool === key ? 'border border-amber-400/20 bg-amber-400/12 text-amber-300' : 'text-white/70 hover:text-white'}`}>
                          {key}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="relative flex gap-3">
                    <div className="hidden w-12 shrink-0 flex-col items-center gap-2 rounded-[1.2rem] border border-white/10 bg-black/30 p-2 sm:flex">
                      {([
                        ['crosshair', '＋'],
                        ['trend', '／'],
                        ['hline', '—'],
                        ['range', '▭'],
                      ] as const).map(([key, icon]) => (
                        <button key={key} onClick={() => { setTool(key); setDraftPoint(null); }} className={`flex h-9 w-9 items-center justify-center rounded-xl border text-sm transition ${tool === key ? 'border-amber-400/25 bg-amber-400/10 text-amber-300' : 'border-white/8 bg-white/[0.02] text-white/65 hover:text-white'}`}>
                          {icon}
                        </button>
                      ))}
                    </div>

                    <div className="relative h-[31rem] flex-1 overflow-hidden rounded-[1.2rem] border border-white/8 bg-black select-none"
                      onContextMenu={(e) => {
                        e.preventDefault();
                        const rect = e.currentTarget.getBoundingClientRect();
                        const p = snapToCandle(normalizePoint(e.clientX, e.clientY, rect));
                        let hitIndex: number | null = null;
                        drawings.forEach((d, i) => {
                          if (d.type === 'hline' && Math.abs(d.y - p.y) < 2) hitIndex = i;
                          if (d.type === 'trend') {
                            const midX = (d.start.x + d.end.x) / 2;
                            const midY = (d.start.y + d.end.y) / 2;
                            if (Math.hypot(midX - p.x, midY - p.y) < 4) hitIndex = i;
                          }
                        });
                        if (hitIndex !== null) {
                          setDrawings((ds) => ds.filter((_, i) => i !== hitIndex));
                          setSelectedDrawing(null);
                          setDragMode(null);
                        }
                      }}
                      onClick={(e) => {
                        if (tool === 'crosshair') return;
                        const rect = e.currentTarget.getBoundingClientRect();
                        const p = snapToCandle(normalizePoint(e.clientX, e.clientY, rect));
                        if (tool === 'hline') { setDrawings((d) => [...d, { type: 'hline', y: p.y }]); return; }
                        if (!draftPoint) { setDraftPoint(p); return; }
                        if (tool === 'trend') setDrawings((d) => [...d, { type: 'trend', start: draftPoint, end: p }]);
                        if (tool === 'range') setDrawings((d) => [...d, { type: 'range', start: draftPoint, end: p }]);
                        setDraftPoint(null);
                      }}
                      onMouseDown={(e) => {
                        const rect = e.currentTarget.getBoundingClientRect();
                        const p = snapToCandle(normalizePoint(e.clientX, e.clientY, rect));
                        if (orderMode) {
                          ['entry', 'stop', 'target'].forEach((key) => {
                            if (Math.abs(orderLevels[key as keyof typeof orderLevels] - p.y) < 2) {
                              setSelectedDrawing(key);
                              setDragMode('order');
                            }
                          });
                        }
                        drawings.forEach((d, i) => {
                          if (d.type === 'hline' && Math.abs(d.y - p.y) < 2) {
                            setSelectedDrawing(i);
                            setDragMode('hline');
                          }
                          if (d.type === 'trend') {
                            const nearStart = Math.hypot(d.start.x - p.x, d.start.y - p.y) < 3;
                            const nearEnd = Math.hypot(d.end.x - p.x, d.end.y - p.y) < 3;
                            const midX = (d.start.x + d.end.x) / 2;
                            const midY = (d.start.y + d.end.y) / 2;
                            const nearMid = Math.hypot(midX - p.x, midY - p.y) < 4;
                            if (nearStart) { setSelectedDrawing(i); setDragMode('trend-start'); }
                            else if (nearEnd) { setSelectedDrawing(i); setDragMode('trend-end'); }
                            else if (nearMid) { setSelectedDrawing(i); setDragMode('trend-move'); }
                          }
                        });
                      }}
                      onMouseMove={(e) => {
                        const rect = e.currentTarget.getBoundingClientRect();
                        const p = snapToCandle(normalizePoint(e.clientX, e.clientY, rect));
                        if (dragMode === 'order' && typeof selectedDrawing === 'string') {
                          setOrderLevels((prev) => ({ ...prev, [selectedDrawing]: p.y }));
                          return;
                        }
                        if (selectedDrawing === null) return;
                        setDrawings((ds) => ds.map((d, i) => {
                          if (i !== selectedDrawing) return d;
                          if (dragMode === 'hline' && d.type === 'hline') return { ...d, y: p.y };
                          if (dragMode === 'trend-start' && d.type === 'trend') return { ...d, start: p };
                          if (dragMode === 'trend-end' && d.type === 'trend') return { ...d, end: p };
                          if (dragMode === 'trend-move' && d.type === 'trend') {
                            const midX = (d.start.x + d.end.x) / 2;
                            const midY = (d.start.y + d.end.y) / 2;
                            const dx = p.x - midX;
                            const dy = p.y - midY;
                            return {
                              ...d,
                              start: { x: Math.max(0, Math.min(100, d.start.x + dx)), y: Math.max(0, Math.min(100, d.start.y + dy)) },
                              end: { x: Math.max(0, Math.min(100, d.end.x + dx)), y: Math.max(0, Math.min(100, d.end.y + dy)) },
                            };
                          }
                          return d;
                        }));
                      }}
                      onMouseUp={() => { setSelectedDrawing(null); setDragMode(null); }}
                      onMouseLeave={() => { setHoveredCandle(null); setSelectedDrawing(null); setDragMode(null); }}
                    >
                      <div className="absolute inset-0 bg-[linear-gradient(to_right,rgba(255,255,255,0.05)_1px,transparent_1px),linear-gradient(to_bottom,rgba(255,255,255,0.05)_1px,transparent_1px)] bg-[size:48px_48px]" />
                      {showLevels && <><div className="absolute inset-x-0 top-[28%] border-t border-amber-400/20" /><div className="absolute inset-x-0 top-[58%] border-t border-white/10" /></>}
                      {orderMode && <><div className="absolute inset-x-0 z-[1]" style={{ bottom: `${Math.min(orderLevels.entry, orderLevels.target)}%`, height: `${Math.abs(orderLevels.target - orderLevels.entry)}%`, background: 'linear-gradient(to top, rgba(74,222,128,0.08), rgba(74,222,128,0.16))' }} /><div className="absolute inset-x-0 z-[1]" style={{ bottom: `${Math.min(orderLevels.entry, orderLevels.stop)}%`, height: `${Math.abs(orderLevels.entry - orderLevels.stop)}%`, background: 'linear-gradient(to top, rgba(248,113,113,0.08), rgba(248,113,113,0.16))' }} /></>}
                      {active && <><div className="absolute top-0 bottom-0 z-10 w-px bg-amber-300/40" style={{ left: `calc(${((hoveredCandle + 0.5) / candles.length) * 100}% )` }} /><div className="absolute left-0 right-0 z-10 h-px bg-amber-300/25" style={{ bottom: `${hoveredPricePct}%` }} /></>}
                      <div className="absolute left-0 right-0 z-10 h-px border-t border-dashed border-amber-300/35" style={{ bottom: `${simulatedPricePct}%` }} />
                      <div className="absolute right-3 z-20 rounded-md border border-amber-400/20 bg-black/90 px-2 py-1 text-[11px] font-medium text-amber-300 shadow-[0_8px_24px_rgba(245,158,11,0.15)]" style={{ bottom: `${simulatedPricePct}%`, transform: 'translateY(50%)' }}>Live {simulatedPrice.toFixed(simulatedPrice > 100 ? 2 : 4)}</div>
                      {active && <div className="absolute right-16 z-20 rounded-md border border-white/10 bg-black/85 px-2 py-1 text-[10px] text-white/70" style={{ bottom: `${hoveredPricePct}%`, transform: 'translateY(50%)' }}>{hoveredPrice.toFixed(hoveredPrice > 100 ? 2 : 4)}</div>}
                      {active && <div className="absolute bottom-2 z-20 rounded-md border border-white/10 bg-black/85 px-2 py-1 text-[10px] text-white/70" style={{ left: `calc(${((hoveredCandle + 0.5) / candles.length) * 100}% - 22px)` }}>{hoveredTime}</div>}

                      <svg className="absolute inset-0 z-10 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none">
                        {enabledIndicators.includes('EMA 20') && candles.length > 1 && (
                          <polyline fill="none" stroke="rgba(96,165,250,0.95)" strokeWidth="0.45" points={ema20Points.map((v, i) => `${((i + 0.5) / candles.length) * 100},${100 - toPct(v)}`).join(' ')} />
                        )}
                        {enabledIndicators.includes('VWAP') && candles.length > 1 && (
                          <polyline fill="none" stroke="rgba(245,158,11,0.95)" strokeWidth="0.42" strokeDasharray="1.2 1.1" points={vwapPoints.map((v, i) => `${((i + 0.5) / candles.length) * 100},${100 - toPct(v)}`).join(' ')} />
                        )}
                        {drawings.map((d, i) => {
                          const isActive = i === selectedDrawing;
                          if (d.type === 'hline') return <line key={i} x1="0" y1={d.y} x2="100" y2={d.y} stroke={isActive ? '#fcd34d' : 'rgba(245,158,11,0.8)'} strokeWidth="0.6" strokeDasharray="2 2" />;
                          if (d.type === 'trend') return <g key={i}><line x1={d.start.x} y1={d.start.y} x2={d.end.x} y2={d.end.y} stroke={isActive ? '#fcd34d' : 'rgba(245,158,11,0.9)'} strokeWidth="0.5" /><circle cx={d.start.x} cy={d.start.y} r="1.2" fill="rgba(245,158,11,0.9)" /><circle cx={d.end.x} cy={d.end.y} r="1.2" fill="rgba(245,158,11,0.9)" /></g>;
                          if (d.type === 'range') {
                            const x = Math.min(d.start.x, d.end.x);
                            const y = Math.min(d.start.y, d.end.y);
                            const w = Math.abs(d.end.x - d.start.x);
                            const h = Math.abs(d.end.y - d.start.y);
                            return <rect key={i} x={x} y={y} width={w} height={h} fill="rgba(245,158,11,0.08)" stroke="rgba(245,158,11,0.8)" strokeWidth="0.4" />;
                          }
                          return null;
                        })}
                        {draftPoint && tool !== 'hline' && <circle cx={draftPoint.x} cy={draftPoint.y} r="1" fill="rgba(245,158,11,0.9)" />}
                      </svg>

                      {orderMode && [{ key: 'entry', label: 'Entry', color: '#fcd34d' }, { key: 'stop', label: 'Stop', color: '#f87171' }, { key: 'target', label: 'Target', color: '#4ade80' }].map((level) => (
                        <div key={level.key} className="absolute inset-x-0 z-20" style={{ bottom: `${orderLevels[level.key as keyof typeof orderLevels]}%` }}>
                          <div className="relative h-px" style={{ backgroundColor: `${level.color}55` }} />
                          <div className="absolute right-3 top-1/2 -translate-y-1/2 rounded-md border border-white/10 bg-black/90 px-2 py-1 text-[11px] font-medium shadow-[0_8px_24px_rgba(0,0,0,0.3)]" style={{ color: level.color }}>{level.label} {fromScreenPct(orderLevels[level.key as keyof typeof orderLevels]).toFixed(2)}</div>
                          <div className="absolute left-3 top-1/2 -translate-y-1/2 h-2.5 w-2.5 rounded-full border border-white/30 bg-black" style={{ boxShadow: `0 0 0 3px ${level.color}20`, backgroundColor: level.color }} />
                        </div>
                      ))}

                      <div className="absolute inset-0 flex items-end gap-[3px] px-4 pb-10 pt-8 pr-16">
                        {candles.map((bar, i) => {
                          const bullish = bar.c >= bar.o;
                          const bodyBottom = Math.min(toPct(bar.o), toPct(bar.c));
                          const bodyHeight = Math.max(Math.abs(toPct(bar.c) - toPct(bar.o)), 2.5);
                          return (
                            <motion.div key={`${bar.t}-${i}`} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }} className="relative h-full flex-1" onMouseEnter={() => setHoveredCandle(i)} onMouseLeave={() => setHoveredCandle(null)} style={{ pointerEvents: 'all' }}>
                              <div className={`absolute left-1/2 w-px -translate-x-1/2 ${bullish ? 'bg-emerald-300/90' : 'bg-red-300/80'}`} style={{ bottom: `${toPct(bar.l)}%`, height: `${toPct(bar.h) - toPct(bar.l)}%` }} />
                              <motion.div className={`absolute left-1/2 -translate-x-1/2 rounded-[2px] ${bullish ? 'bg-gradient-to-t from-emerald-500 to-emerald-300' : 'bg-gradient-to-t from-red-500 to-red-300'} w-[28%]`} animate={{ bottom: `${bodyBottom}%`, height: `${bodyHeight}%` }} transition={{ duration: 0.35, ease: 'easeOut' }} />
                              {enabledIndicators.includes('Volume') && <motion.div className={`absolute left-1/2 -translate-x-1/2 ${bullish ? 'bg-emerald-400/35' : 'bg-red-400/30'} w-[38%] rounded-t-[2px]`} animate={{ height: `${10 + (i % 5) * 2.5}%` }} transition={{ duration: 0.35, ease: 'easeOut' }} style={{ bottom: '0%' }} />}
                              <div className="absolute inset-0 cursor-crosshair" />
                            </motion.div>
                          );
                        })}
                      </div>

                      <div className="absolute left-4 top-4 rounded-full border border-white/10 bg-black/55 px-3 py-1.5 text-[11px] text-white/60">{profile.venue} • {sessionMode} • {profile.pct}</div>
                      <div className="absolute left-4 top-14 flex flex-wrap gap-2 text-[10px]">
                        {enabledIndicators.includes('EMA 20') && <span className="rounded-full border border-blue-400/20 bg-blue-400/10 px-2 py-1 text-blue-300">EMA 20</span>}
                        {enabledIndicators.includes('VWAP') && <span className="rounded-full border border-amber-400/20 bg-amber-400/10 px-2 py-1 text-amber-300">VWAP</span>}
                        {enabledIndicators.includes('Volume') && <span className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-2 py-1 text-emerald-300">VOL</span>}
                      </div>
                      <div className="absolute right-4 top-4 rounded-full border border-emerald-400/20 bg-emerald-400/10 px-3 py-1.5 text-[11px] text-emerald-400">{(simulatedPrice - entryPrice) * 3 >= 0 ? '+' : ''}{unrealizedPnL} PnL</div>
                      <div className="absolute left-4 bottom-12 rounded-full border border-white/10 bg-black/55 px-3 py-1.5 text-[11px] text-white/58">Tool: {tool} • T / H / R / Esc</div>
                      {hotkeyToast && <div className="absolute left-1/2 top-4 z-30 -translate-x-1/2 rounded-full border border-amber-400/20 bg-black/85 px-3 py-1.5 text-[11px] text-amber-300">{hotkeyToast}</div>}
                      {executionFlash && <div className="absolute left-1/2 top-14 z-30 -translate-x-1/2 rounded-full border border-emerald-400/20 bg-emerald-400/10 px-3 py-1.5 text-[11px] text-emerald-300">{executionFlash}</div>}
                      <button onClick={() => { setDrawings([]); setDraftPoint(null); }} className="absolute right-16 bottom-12 rounded-full border border-white/10 bg-black/55 px-3 py-1.5 text-[11px] text-white/60 hover:text-white">Clear drawings</button>
                      <div className="absolute inset-y-0 right-0 z-20 w-16 border-l border-white/8 bg-black/55">
                        {priceScale.map((price, idx) => <div key={price} className="absolute right-2 text-[10px] text-white/65" style={{ top: `${8 + idx * 10.5}%`, transform: 'translateY(-50%)' }}>{price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}</div>)}
                      </div>
                      <div className="absolute inset-x-0 bottom-0 z-20 h-10 border-t border-white/8 bg-black/45 px-4 pr-16">
                        <div className="flex h-full items-center justify-between text-[10px] text-white/50">{bottomLabels.map((label, idx) => <span key={`${label}-${idx}`}>{label}</span>)}</div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div className="grid gap-4">
              <div className="rounded-[1.4rem] border border-white/10 bg-black/28 p-4">
                <details open className="group">
                  <summary className="flex cursor-pointer list-none items-center justify-between rounded-xl border border-white/8 bg-white/[0.02] px-4 py-3 text-left">
                    <div><div className="text-[11px] uppercase tracking-[0.28em] text-white/40">Watchlist</div><div className="mt-1 text-sm text-white/65">Futures lineup</div></div>
                    <span className="text-white/55 transition group-open:rotate-180">⌄</span>
                  </summary>
                  <div className="mt-3 max-h-[18rem] space-y-4 overflow-y-auto pr-1 text-sm">
                    {watchlist.map((section) => (
                      <div key={section.group}>
                        <div className="mb-2 text-[10px] uppercase tracking-[0.26em] text-white/35">{section.group}</div>
                        <div className="space-y-2">
                          {section.items.map((s, idx) => {
                            const p = symbolProfiles[s];
                            const last = Number((p.base + p.vol * 0.15).toFixed(4));
                            const change = p.pct;
                            return (
                              <button key={s} onClick={() => setSymbol(s)} className={`group flex w-full items-center justify-between rounded-xl border border-white/8 bg-white/[0.02] px-3 py-2 text-left transition hover:bg-white/[0.04] ${symbol === s ? 'ring-1 ring-amber-400/30 bg-amber-400/[0.06]' : ''}`}>
                                <div className="flex items-center gap-2"><span className={`font-medium ${symbol === s ? 'text-amber-300' : 'text-white'}`}>{s}</span><svg viewBox="0 0 40 12" className="h-3 w-10 opacity-55 transition group-hover:opacity-90"><path d={idx % 2 === 0 ? 'M1 10 C8 9, 10 7, 16 6 C22 5, 26 7, 31 4 C34 3, 37 2, 39 2' : 'M1 9 C6 7, 11 8, 16 5 C21 3, 26 4, 31 6 C35 7, 37 5, 39 3'} fill="none" stroke="currentColor" strokeWidth="1.4" className="text-amber-300" /></svg></div>
                                <div className="text-right"><div className="text-[11px] text-white/70">{last.toFixed(last > 100 ? 2 : 4)}</div><div className="text-[11px] text-emerald-400">{change}</div></div>
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                </details>
              </div>

              <div className="rounded-[1.4rem] border border-white/10 bg-black/28 p-4">
                <div className="text-[11px] uppercase tracking-[0.28em] text-white/40">Order panel</div>
                <div className="mt-4 space-y-3">
                  {[
                    ['Position size', '3 contracts'],
                    ['Entry', fromScreenPct(orderLevels.entry).toFixed(2)],
                    ['Stop', fromScreenPct(orderLevels.stop).toFixed(2)],
                    ['Target', fromScreenPct(orderLevels.target).toFixed(2)],
                    ['Live Price', simulatedPrice.toFixed(simulatedPrice > 100 ? 2 : 4)],
                    ['Unrealized', `${(simulatedPrice - entryPrice) * 3 >= 0 ? '+' : ''}${unrealizedPnL}`],
                    ['R Multiple', `${((fromScreenPct(orderLevels.target) - fromScreenPct(orderLevels.entry)) / Math.max(Math.abs(fromScreenPct(orderLevels.entry) - fromScreenPct(orderLevels.stop)), 0.0001)).toFixed(2)}R`],
                  ].map(([label, value]) => (
                    <div key={label} className="rounded-xl border border-white/8 bg-white/[0.02] px-3 py-3"><div className="text-[11px] uppercase tracking-[0.24em] text-white/38">{label}</div><div className="mt-1 text-base font-semibold text-white">{value}</div></div>
                  ))}
                </div>
                <button onClick={() => setOrderMode((v) => !v)} className={`mt-3 w-full rounded-full border px-4 py-3 text-sm font-semibold ${orderMode ? 'border-amber-400/25 bg-amber-400/10 text-amber-300' : 'border-white/10 bg-white/[0.03] text-white/70'}`}>{orderMode ? 'Order-on-chart enabled' : 'Enable order-on-chart'}</button>
                <div className="mt-4 grid grid-cols-2 gap-3"><button onClick={() => handleExecution('BUY')} className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-4 py-3 text-sm font-semibold text-emerald-400">Buy</button><button onClick={() => handleExecution('SELL')} className="rounded-full border border-red-400/20 bg-red-400/10 px-4 py-3 text-sm font-semibold text-red-400">Sell</button></div>
              </div>

              <div className="rounded-[1.4rem] border border-white/10 bg-black/28 p-4">
                <div className="flex items-center justify-between"><div className="text-[11px] uppercase tracking-[0.28em] text-white/40">Time & Sales</div><div className="text-[10px] text-white/35">Live tape</div></div>
                <div className="mt-3 max-h-[14rem] overflow-y-auto space-y-2 pr-1">
                  {Array.from({ length: 10 }).map((_, i) => {
                    const price = Number((simulatedPrice + (Math.random() - 0.5) * profile.vol * 0.12).toFixed(4));
                    const size = Math.floor(Math.random() * 8) + 1;
                    const sideUp = i % 2 === 0;
                    return <div key={i} className="grid grid-cols-[1fr_auto_auto] gap-3 rounded-lg border border-white/6 bg-white/[0.02] px-3 py-2 text-[11px]"><span className="text-white/45">{new Date(Date.now() - i * 2200).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span><span className={sideUp ? 'text-emerald-400' : 'text-red-400'}>{price.toFixed(price > 100 ? 2 : 4)}</span><span className="text-white/65">{size}</span></div>;
                  })}
                </div>
              </div>

              <div className="rounded-[1.4rem] border border-white/10 bg-black/28 p-4">
                <div className="flex items-center justify-between"><div className="text-[11px] uppercase tracking-[0.28em] text-white/40">Tradetaur Sync</div><div className="text-[10px] text-amber-300">Execution feed</div></div>
                <div className="mt-3 max-h-[12rem] overflow-y-auto space-y-2 pr-1">
                  {tradeLog.length === 0 ? <div className="rounded-lg border border-white/6 bg-white/[0.02] px-3 py-3 text-[11px] text-white/45">Execute a trade to sync it into Tradetaur.</div> : tradeLog.map((trade, i) => <div key={`${trade.time}-${i}`} className="rounded-lg border border-white/6 bg-white/[0.02] px-3 py-3 text-[11px]"><div className="flex items-center justify-between"><span className={trade.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}>{trade.side}</span><span className="text-white/45">{trade.time}</span></div><div className="mt-1 text-white/75">{trade.symbol} @ {trade.price}</div><div className="mt-1 text-white/40">Sent to review engine</div></div>)}
                </div>
              </div>

              <div className="rounded-[1.4rem] border border-white/10 bg-black/28 p-4 text-sm leading-7 text-white/62">
                <div className="mb-2 text-[11px] uppercase tracking-[0.28em] text-white/40">Tradovate / broker integration</div>
                This scaffold includes a ready API route for generating a Tradovate OAuth URL. Add your Tradovate env vars, then wire your callback page and websocket trade sync next.
              </div>
            </div>
          </motion.div>
        </div>
      )}
    </div>
  );
}
