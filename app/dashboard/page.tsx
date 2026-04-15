import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";

export default async function DashboardPage() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) redirect("/login");

  const { data: profile } = await supabase
    .from("profiles")
    .select("tier, full_name, email")
    .eq("id", user.id)
    .single();

  return (
    <div className="min-h-screen bg-[#04060a] text-white p-8">
      <div className="max-w-7xl mx-auto">
        <div className="text-[10px] uppercase tracking-[0.34em] text-[#d7b56d]">Private Dashboard</div>
        <h1 className="mt-2 text-4xl md:text-5xl font-semibold tracking-[-0.04em]">Welcome back</h1>
        <p className="mt-3 text-white/56 max-w-2xl">
          Signed in as {profile?.full_name ?? user.email} • Tier: {profile?.tier ?? "foundation"}
        </p>
      </div>
    </div>
  );
}
