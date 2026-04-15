# Northview

Vercel-ready Next.js + Stripe + Supabase scaffold.

## Setup

1. Copy `.env.local.example` to `.env.local`
2. Fill in Supabase + Stripe keys
3. Run:
   - `npm install`
   - `npm run dev`

## Supabase SQL

```sql
create table public.profiles (
  id uuid primary key,
  email text unique,
  full_name text,
  tier text not null default 'foundation',
  stripe_customer_id text unique,
  created_at timestamp with time zone default now()
);
```
