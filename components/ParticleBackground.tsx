'use client';

import { motion } from 'framer-motion';

export default function ParticleBackground() {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      <div className="absolute inset-0 bg-black" />
      <motion.div
        className="absolute inset-0 opacity-90"
        animate={{ backgroundPosition: ['0% 0%', '14% 10%', '0% 0%'] }}
        transition={{ duration: 36, repeat: Infinity, ease: 'linear' }}
        style={{
          backgroundImage:
            'radial-gradient(circle at 12% 22%, rgba(245,158,11,0.55) 0 1px, transparent 1.6px), radial-gradient(circle at 76% 18%, rgba(245,158,11,0.45) 0 1px, transparent 1.6px), radial-gradient(circle at 58% 68%, rgba(245,158,11,0.36) 0 1px, transparent 1.6px), radial-gradient(circle at 26% 74%, rgba(245,158,11,0.28) 0 1px, transparent 1.6px)',
          backgroundSize: '120px 120px, 160px 160px, 180px 180px, 220px 220px',
        }}
      />
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_30%,rgba(245,158,11,0.14),transparent_24%),linear-gradient(to_bottom,rgba(0,0,0,0.05),rgba(0,0,0,0.45))]" />
    </div>
  );
}
