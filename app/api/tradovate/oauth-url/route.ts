import { NextResponse } from 'next/server';
import { buildTradovateOAuthUrl } from '@/lib/tradovate';

export async function GET() {
  try {
    const url = buildTradovateOAuthUrl();
    return NextResponse.json({ url });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Failed to build Tradovate OAuth URL' },
      { status: 500 }
    );
  }
}
