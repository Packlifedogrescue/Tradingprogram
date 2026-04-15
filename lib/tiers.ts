export const TIER_RANK = {
  foundation: 0,
  pro: 1,
  elite: 2,
} as const;

export type Tier = keyof typeof TIER_RANK;

export function canAccess(current: Tier, required: Tier) {
  return TIER_RANK[current] >= TIER_RANK[required];
}

export const tierConfig = {
  foundation: {
    label: "Foundation",
    price: "$19/mo",
    cta: "Start Foundation",
  },
  pro: {
    label: "Pro",
    price: "$79/mo",
    cta: "Upgrade to Pro",
  },
  elite: {
    label: "Elite",
    price: "$249/mo",
    cta: "Upgrade to Elite",
  },
} as const;
