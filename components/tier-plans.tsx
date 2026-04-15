"use client";

import { motion } from "framer-motion";
import { Tier, tierConfig, TIER_RANK } from "@/lib/tiers";

export function TierPlans({ currentTier, onUpgrade }: { currentTier: Tier; onUpgrade: (tier: Tier) => void }) {
  return (
    <div className="grid lg:grid-cols-3 gap-6">
      {Object.entries(tierConfig).map(([key, plan]) => (
        <div key={key} className={`rounded-[30px] p-8 border ${key === "pro" ? "border-[#d7b56d]/30 bg-[linear-gradient(180deg,rgba(215,181,109,0.13),rgba(255,255,255,0.04))]" : "border-white/8 bg-white/[0.03]"}`}>
          <h3 className="text-2xl font-medium tracking-tight">{plan.label}</h3>
          <div className="text-4xl font-semibold mt-4 tracking-tight">{plan.price}</div>
          {TIER_RANK[key as Tier] > TIER_RANK[currentTier] && (
            <motion.button
              onClick={() => onUpgrade(key as Tier)}
              className="mt-8 inline-flex w-full justify-center rounded-full bg-[#d7b56d] px-5 py-4 text-sm font-semibold uppercase tracking-[0.12em] text-black"
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              {plan.cta}
            </motion.button>
          )}
        </div>
      ))}
    </div>
  );
}
