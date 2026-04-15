"use client";

import { useState } from "react";
import { BillingModal } from "@/components/billing-modal";
import { CrestLogo } from "@/components/crest-logo";
import { LiveMarketGraph } from "@/components/live-market-graph";
import { TierPlans } from "@/components/tier-plans";
import type { Tier } from "@/lib/tiers";

export default function HomePage() {
  const [billingOpen, setBillingOpen] = useState(false);
  const [selectedPlan, setSelectedPlan] = useState<Tier>("pro");

  async function startCheckout(plan: Tier) {
    const userId = "demo-user";
    const email = "demo@example.com";

    const res = await fetch("/api/checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan, userId, email }),
    });

    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Checkout failed");
    window.location.href = data.url;
  }

  return (
    <div className="min-h-screen bg-[#05070b] text-white overflow-x-hidden">
      <BillingModal
        open={billingOpen}
        onClose={() => setBillingOpen(false)}
        selectedPlan={selectedPlan}
        onCheckout={startCheckout}
      />

      <header className="sticky top-0 z-40 border-b border-white/8 bg-[#05070b]/80 backdrop-blur-2xl">
        <div className="max-w-7xl mx-auto px-6 py-5 flex items-center justify-between">
          <CrestLogo />
          <button onClick={() => { setSelectedPlan("pro"); setBillingOpen(true); }} className="rounded-full border border-[#d7b56d]/30 bg-[#d7b56d]/10 px-5 py-2.5 text-sm font-medium text-[#f1d79a]">
            Compare Plans
          </button>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-24 space-y-20">
        <section className="grid lg:grid-cols-[1.05fr_0.95fr] gap-12 items-center">
          <div>
            <div className="inline-flex items-center rounded-full border border-white/10 bg-white/[0.03] px-4 py-2 text-[11px] uppercase tracking-[0.28em] text-[#d7b56d] mb-7">Tiered Trading Platform</div>
            <h1 className="text-5xl md:text-7xl font-semibold leading-[0.92] tracking-[-0.04em] max-w-5xl">See the market clearly. Trade with structure.</h1>
            <p className="mt-7 max-w-2xl text-lg md:text-xl text-white/62 leading-relaxed">Northview is a premium trading platform with tiered chart access, portfolio visibility, alerts, and a private decision environment.</p>
            <div className="mt-10 flex flex-col sm:flex-row gap-4">
              <a href="/dashboard" className="rounded-full bg-[#d7b56d] text-black px-7 py-4 text-sm font-semibold tracking-[0.12em] uppercase text-center">Enter Dashboard</a>
              <button onClick={() => { setSelectedPlan("pro"); setBillingOpen(true); }} className="rounded-full border border-white/10 bg-white/[0.03] px-7 py-4 text-sm font-semibold tracking-[0.12em] uppercase text-white/80">Compare Plans</button>
            </div>
          </div>
          <div>
            <LiveMarketGraph />
          </div>
        </section>

        <section>
          <div className="text-center max-w-3xl mx-auto mb-12">
            <div className="text-[11px] uppercase tracking-[0.34em] text-[#d7b56d] mb-4">Membership Tiers</div>
            <h2 className="text-4xl md:text-5xl font-semibold tracking-[-0.04em]">Foundation, Pro, and Elite</h2>
          </div>
          <TierPlans currentTier="foundation" onUpgrade={(tier) => { setSelectedPlan(tier); setBillingOpen(true); }} />
        </section>
      </main>
    </div>
  );
}
