const TRADOVATE_OAUTH_AUTHORIZE = 'https://trader.tradovate.com/oauth';

export function buildTradovateOAuthUrl() {
  const clientId = process.env.TRADOVATE_CLIENT_ID;
  const redirectUri = process.env.TRADOVATE_REDIRECT_URI;

  if (!clientId || !redirectUri) {
    throw new Error('Missing TRADOVATE_CLIENT_ID or TRADOVATE_REDIRECT_URI');
  }

  const params = new URLSearchParams({
    client_id: clientId,
    response_type: 'code',
    redirect_uri: redirectUri,
    scope: 'openid profile trade marketdata',
  });

  return `${TRADOVATE_OAUTH_AUTHORIZE}?${params.toString()}`;
}
