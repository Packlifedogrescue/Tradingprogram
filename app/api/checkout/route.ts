import { NextRequest, NextResponse } from "next/server";
import { stripe, PRICE_MAP } from "@/lib/stripe";
import { createClient } from "@supabase/supabase-js";

export async function POST(req: NextRequest) {
  const { plan, userId, email } = await req.json();

  if (!plan || !PRICE_MAP[plan as keyof typeof PRICE_MAP]) {
    return NextResponse.json({ error: "Invalid plan" }, { status: 400 });
  }

  const supabase = createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_ROLE_KEY!
  );

  const { data: profile } = await supabase
    .from("profiles")
    .select("stripe_customer_id")
    .eq("id", userId)
    .maybeSingle();

  let customerId = profile?.stripe_customer_id ?? null;

  if (!customerId) {
    const customer = await stripe.customers.create({
      email,
      metadata: { userId },
    });
    customerId = customer.id;

    await supabase
      .from("profiles")
      .upsert({ id: userId, email, stripe_customer_id: customerId }, { onConflict: "id" });
  }

  const session = await stripe.checkout.sessions.create({
    mode: "subscription",
    customer: customerId,
    line_items: [{ price: PRICE_MAP[plan as keyof typeof PRICE_MAP], quantity: 1 }],
    success_url: `${process.env.NEXT_PUBLIC_APP_URL}/dashboard?checkout=success`,
    cancel_url: `${process.env.NEXT_PUBLIC_APP_URL}/dashboard?checkout=cancelled`,
    metadata: { userId, plan },
  });

  return NextResponse.json({ url: session.url });
}
