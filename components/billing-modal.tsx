"use client";

import { AnimatePresence, motion } from "framer-motion";
import { Tier, tierConfig } from "@/lib/tiers";

export function BillingModal({
  open,
  onClose,
  selectedPlan,
  onCheckout,
}: {
  open: boolean;
  onClose: () => void;
  selectedPlan: Tier;
  onCheckout: (plan: Tier) => Promise<void>;
}) {
  if (!open) return null;

  return (
    <AnimatePresence>
      <motion.div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/70 p-6" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
        <motion.div className="w-full max-w-5xl rounded-[34px] border border-white/10 bg-[#0a0d13] overflow-hidden" initial={{ opacity: 0, y: 24 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 12 }}>
          <div className="grid lg:grid-cols-[0.9fr_1.1fr]">
            <div className="border-r border-white/8 bg-[linear-gradient(180deg,rgba(215,181,109,0.08),rgba(255,255,255,0.02))] p-8">
              <div className="text-[11px] uppercase tracking-[0.32em] text-[#d7b56d]">Upgrade Access</div>
              <h3 className="mt-3 text-3xl font-semibold tracking-[-0.04em]">Unlock your next trading tier</h3>
              <div className="mt-8 space-y-4">
                {Object.entries(tierConfig).map(([key, plan]) => (
                  <div key={key} className={`rounded-[22px] border p-5 ${key === selectedPlan ? "border-[#d7b56d]/25 bg-[#d7b56d]/10" : "border-white/8 bg-white/[0.03]"}`}>
                    <div className="text-lg font-semibold">{plan.label} • {plan.price}</div>
                  </div>
                ))}
              </div>
            </div>
            <div className="p-8">
              <div className="flex items-center justify-between mb-6">
                <div>
                  <div className="text-[11px] uppercase tracking-[0.28em] text-[#d7b56d]">Secure Checkout</div>
                  <div className="mt-2 text-2xl font-semibold">Complete upgrade</div>
                </div>
                <button onClick={onClose} className="rounded-full border border-white/10 bg-white/[0.03] px-4 py-2 text-sm text-white/70">Close</button>
              </div>
              <motion.button
                className="w-full rounded-full bg-[#d7b56d] px-5 py-4 text-sm font-semibold uppercase tracking-[0.12em] text-black"
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
                onClick={() => onCheckout(selectedPlan)}
              >
                Continue to Secure Billing
              </motion.button>
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
