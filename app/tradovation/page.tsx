import Header from '@/components/Header';
import ParticleBackground from '@/components/ParticleBackground';
import Tradovation from '@/components/Tradovation';

export default function TradovationPage() {
  return (
    <div className="relative min-h-screen overflow-hidden bg-black text-white">
      <ParticleBackground />
      <div className="relative z-10">
        <Header />
        <Tradovation />
      </div>
    </div>
  );
}
