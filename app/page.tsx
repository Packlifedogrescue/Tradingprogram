import Header from '@/components/Header';
import HomeHero from '@/components/HomeHero';
import ParticleBackground from '@/components/ParticleBackground';

export default function HomePage() {
  return (
    <div className="relative min-h-screen overflow-hidden bg-black text-white">
      <ParticleBackground />
      <div className="relative z-10">
        <Header />
        <HomeHero />
      </div>
    </div>
  );
}
