"use client";

import { motion } from "framer-motion";
import { Tier, tierConfig } from "@/lib/tiers";

export function UpgradePrompt({
  title,
  description,
  requiredTier,
  onUpgrade,
}: {
  title: string;
  description: string;
  requiredTier: Tier;
  onUpgrade: (tier: Tier) => void;
}) {
  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/55 backdrop-blur-sm">
      <div className="max-w-md rounded-[28px] border border-white/10 bg-[#0a0d13] p-6 shadow-[0_20px_80px_rgba(0,0,0,0.45)]">
        <div className="text-[10px] uppercase tracking-[0.24em] text-[#d7b56d]">Feature Locked</div>
        <h4 className="mt-3 text-2xl font-semibold">{title}</h4>
        <p className="mt-3 text-sm leading-relaxed text-white/58">{description}</p>
        <div className="mt-4 rounded-[18px] border border-[#d7b56d]/20 bg-[#d7b56d]/8 p-4">
          <div className="text-sm text-white/70">Unlock with {tierConfig[requiredTier].label}</div>
          <div className="mt-1 text-xl font-semibold text-[#f1d79a]">{tierConfig[requiredTier].price}</div>
        </div>
        <motion.button
          className="mt-5 w-full rounded-full bg-[#d7b56d] px-5 py-3 text-sm font-semibold uppercase tracking-[0.12em] text-black"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
          onClick={() => onUpgrade(requiredTier)}
        >
          Upgrade to {tierConfig[requiredTier].label}
        </motion.button>
      </div>
    </div>
  );
}
