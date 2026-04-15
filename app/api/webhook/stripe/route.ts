import { headers } from "next/headers";
import { stripe } from "@/lib/stripe";
import { createClient } from "@supabase/supabase-js";

function priceIdToTier(priceId: string) {
  if (priceId === process.env.STRIPE_PRICE_FOUNDATION) return "foundation";
  if (priceId === process.env.STRIPE_PRICE_PRO) return "pro";
  if (priceId === process.env.STRIPE_PRICE_ELITE) return "elite";
  return "foundation";
}

export async function POST(req: Request) {
  const body = await req.text();
  const sig = (await headers()).get("stripe-signature");

  if (!sig) return new Response("Missing signature", { status: 400 });

  let event;
  try {
    event = stripe.webhooks.constructEvent(body, sig, process.env.STRIPE_WEBHOOK_SECRET!);
  } catch {
    return new Response("Invalid signature", { status: 400 });
  }

  const supabase = createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_ROLE_KEY!
  );

  if (
    event.type === "checkout.session.completed" ||
    event.type === "customer.subscription.updated" ||
    event.type === "customer.subscription.created"
  ) {
    const obj = event.data.object as any;
    const customerId = obj.customer;
    const priceId = obj.items?.data?.[0]?.price?.id ?? obj.lines?.data?.[0]?.price?.id ?? null;

    if (customerId && priceId) {
      await supabase
        .from("profiles")
        .update({ tier: priceIdToTier(priceId) })
        .eq("stripe_customer_id", customerId);
    }
  }

  if (event.type === "customer.subscription.deleted" || event.type === "invoice.payment_failed") {
    const obj = event.data.object as any;
    const customerId = obj.customer;
    if (customerId) {
      await supabase
        .from("profiles")
        .update({ tier: "foundation" })
        .eq("stripe_customer_id", customerId);
    }
  }

  return new Response("ok");
}
