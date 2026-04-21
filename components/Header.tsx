import Link from 'next/link';

export default function Header() {
  return (
    <header className="sticky top-0 z-50 border-b border-white/10 bg-black/70 backdrop-blur-xl">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-6 px-6 py-5 lg:px-8">
        <Link href="/" className="text-xl font-semibold tracking-wide text-white">
          TRAD<span className="text-amber-300">ETAUR</span>
        </Link>
        <nav className="hidden items-center gap-5 text-sm text-white/75 md:flex">
          <Link href="/">Home</Link>
          <Link href="/tradovation">Tradovation</Link>
        </nav>
        <Link href="/tradovation" className="rounded-full bg-amber-400 px-5 py-2.5 text-sm font-semibold text-black">
          Open Platform
        </Link>
      </div>
    </header>
  );
}
