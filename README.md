# Tradetaur / Tradovation Vercel Build

This is a deploy-ready Next.js scaffold for Vercel.

## Included
- Tradovation custom live-simulated chart page
- Optional TradingView Advanced Chart widget toggle
- Tradovate OAuth URL API route stub
- Tailwind + Framer Motion setup

## Deploy
1. Upload this folder to GitHub.
2. Import the repo into Vercel.
3. Add the env vars from `.env.example`.
4. Deploy.

## Local dev
```bash
npm install
npm run dev
```

## Notes
- The TradingView widget uses the public embed script.
- For a deeper TradingView business integration, replace the widget with Advanced Charts + your own datafeed.
- The Tradovate route currently only builds an OAuth URL. You still need to implement the callback exchange and account/websocket sync.
