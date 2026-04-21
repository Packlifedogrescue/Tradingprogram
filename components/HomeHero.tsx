import Link from 'next/link';

export default function HomeHero() {
  return (
    <main className="relative mx-auto grid max-w-7xl items-center gap-12 px-6 py-24 lg:grid-cols-[1fr_0.9fr] lg:px-8">
      <div className="relative z-10">
        <div className="inline-flex rounded-full border border-amber-400/20 bg-amber-400/10 px-4 py-2 text-xs font-semibold uppercase tracking-[0.32em] text-amber-300">
          Performance command center
        </div>
        <h1 className="mt-6 max-w-[11ch] text-5xl font-semibold leading-[0.95] tracking-[-0.05em] text-white sm:text-6xl lg:text-7xl">
          Structure. Performance. Edge.
        </h1>
        <p className="mt-6 max-w-2xl text-lg leading-8 text-white/62">
          Tradetaur brings broker sync, journal intelligence, and live execution context into one premium trading workflow.
        </p>
        <div className="mt-10 flex flex-wrap gap-4">
          <Link href="/tradovation" className="rounded-full bg-amber-400 px-8 py-4 text-sm font-semibold text-black">
            Launch Tradovation
          </Link>
          <a href="#deploy" className="rounded-full border border-white/10 bg-white/[0.03] px-8 py-4 text-sm font-semibold text-white/80">
            Deployment Notes
          </a>
        </div>
      </div>
      <div className="relative z-10 rounded-[2rem] border border-white/10 bg-black/35 p-8 shadow-[0_40px_120px_rgba(245,158,11,0.16)]">
        <div className="text-xs uppercase tracking-[0.32em] text-amber-300">Included in this build</div>
        <div className="mt-6 grid gap-3 text-sm text-white/72 sm:grid-cols-2">
          {['Tradovation live chart UI', 'TradingView embed page', 'Tradovate OAuth stub', 'Deploy-ready Next.js structure', 'Tailwind setup', 'Vercel-ready env file'].map((item) => (
            <div key={item} className="rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-4">
              {item}
            </div>
          ))}
        </div>
        <div id="deploy" className="mt-8 rounded-2xl border border-white/10 bg-black/40 p-5 text-sm leading-7 text-white/60">
          Push this folder to GitHub, import the repo into Vercel, add your env vars, and deploy.
        </div>
      </div>
    </main>
  );
}
