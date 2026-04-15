export default function LoginPage() {
  return (
    <div className="min-h-screen bg-[#05070b] text-white flex items-center justify-center px-6">
      <div className="w-full max-w-md rounded-[30px] border border-white/8 bg-white/[0.03] p-8">
        <div className="text-[11px] uppercase tracking-[0.28em] text-[#d7b56d]">Login</div>
        <h1 className="mt-3 text-3xl font-semibold">Sign in</h1>
        <p className="mt-3 text-white/58">Wire this to Supabase auth UI or your own email/password flow.</p>
      </div>
    </div>
  );
}
