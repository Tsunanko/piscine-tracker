/**
 * Cloudflare Workers - 42 Intra OAuth認証プロキシ
 *
 * 動作：
 * 1. /login → 42 Intra の Authorization Code Flow を開始
 * 2. /auth/callback → codeをトークンに交換、campus_id チェック（42 Tokyo = 26）
 * 3. OK → GitHub Pages の auth-callback.html に access_token をハッシュで渡す
 * 4. NG（他キャンパス）→ アクセス拒否画面
 *
 * KV不要・Cookie管理不要（トークンはクライアント側のsessionStorageで管理）
 */

const GITHUB_PAGES_URL = 'https://tsunanko.github.io/piscine-tracker';
const CAMPUS_ID_TOKYO  = 26;

const AUTH_URL     = 'https://api.intra.42.fr/oauth/authorize';
const TOKEN_URL    = 'https://api.intra.42.fr/oauth/token';
const USERINFO_URL = 'https://api.intra.42.fr/v2/me';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': 'https://tsunanko.github.io',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, X-Admin-Secret, Authorization',
};

/** 管理者かどうか検証する（2つの認証方式をサポート）
 *  1. X-Admin-Secret ヘッダー（curl等の直接アクセス向け）
 *  2. Authorization: Bearer <token>（ブラウザログイン済みijoja向け）
 *     - 'piscine:ijoja' → simple loginのijoja
 *     - 42 access token → 42 APIで検証してloginがijojaか確認
 */
async function isAdmin(request, env) {
  // 1. X-Admin-Secret による認証
  const secret = request.headers.get('X-Admin-Secret');
  if (env.ADMIN_SECRET && secret === env.ADMIN_SECRET) return true;

  // 2. Authorization: Bearer による認証
  const auth = request.headers.get('Authorization');
  if (!auth) return false;
  const token = auth.replace(/^Bearer\s+/, '');

  // simple login の ijoja
  if (token === 'piscine:ijoja') return true;

  // 42 OAuth token → 42 API で検証
  try {
    const res = await fetch(USERINFO_URL, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.ok) {
      const user = await res.json();
      return user.login === 'ijoja';
    }
  } catch {}
  return false;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // CORS プリフライト
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    if (url.pathname === '/auth/callback') {
      return handleCallback(request, env, url);
    }

    if (url.pathname === '/login') {
      return startOAuth(env);
    }

    // ─── ログイン記録 API ───────────────────────────
    if (url.pathname === '/api/log' && request.method === 'POST') {
      return handleLog(request, env);
    }

    // ─── 同意記録 API ────────────────────────────
    if (url.pathname === '/api/consent' && request.method === 'POST') {
      return handleConsent(request, env);
    }

    // ─── ログ閲覧 API（管理者のみ）────────────────
    if (url.pathname === '/api/logs' && request.method === 'GET') {
      return handleGetLogs(request, env);
    }

    // ─── 同意記録一覧 API（管理者のみ）────────────
    if (url.pathname === '/api/consents' && request.method === 'GET') {
      return handleGetConsents(request, env);
    }

    // ルート (/) → ログイン画面
    return loginPage();
  }
};

/** ログイン記録を KV に保存 */
async function handleLog(request, env) {
  try {
    const body = await request.json();
    const { login, method } = body;
    if (!login || !method) {
      return new Response('Bad Request', { status: 400, headers: CORS_HEADERS });
    }

    const jst = new Date(Date.now() + 9 * 3600 * 1000);
    const ts  = jst.toISOString().replace('T', ' ').slice(0, 19) + ' JST';
    const ip  = request.headers.get('CF-Connecting-IP') || 'unknown';
    const ua  = request.headers.get('User-Agent') || '';

    const entry = { login, method, ts, ip, ua };
    // KV key: log:YYYYMMDD_HHMMSS_login（時系列でソート可能）
    const key = `log:${jst.toISOString().replace(/[^0-9]/g, '').slice(0, 14)}_${login}`;
    await env.LOGIN_LOGS.put(key, JSON.stringify(entry)); // 無期限保持

    return new Response('OK', { status: 200, headers: CORS_HEADERS });
  } catch (e) {
    return new Response('Error', { status: 500, headers: CORS_HEADERS });
  }
}

/** ログ一覧を返す（管理者のみ）- JSON形式 */
async function handleGetLogs(request, env) {
  if (!await isAdmin(request, env)) {
    return new Response(JSON.stringify({ error: 'Unauthorized' }), {
      status: 401,
      headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
    });
  }

  // KVのlistは最大1000件。大量になった場合は cursor で続きを取得
  const allKeys = [];
  let cursor = undefined;
  do {
    const result = await env.LOGIN_LOGS.list({ prefix: 'log:', cursor });
    allKeys.push(...result.keys);
    cursor = result.list_complete ? undefined : result.cursor;
  } while (cursor);

  const entries = await Promise.all(
    allKeys.map(async k => {
      const val = await env.LOGIN_LOGS.get(k.name);
      try { return JSON.parse(val); } catch { return null; }
    })
  );
  const logs = entries.filter(Boolean).reverse(); // 新しい順

  return new Response(JSON.stringify(logs), {
    headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
  });
}

/** 同意記録を保存（login単位で最新を上書き）*/
async function handleConsent(request, env) {
  try {
    const body = await request.json();
    const { login, consentedAt, method } = body;
    if (!login) {
      return new Response(JSON.stringify({ error: 'Bad Request' }), {
        status: 400,
        headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
      });
    }
    const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
    const entry = { login, consentedAt: consentedAt || new Date().toISOString(), method: method || 'unknown', ip };
    // login単位でキーを固定（最新の同意で上書き）
    await env.LOGIN_LOGS.put(`consent:${login}`, JSON.stringify(entry));
    return new Response('OK', { status: 200, headers: CORS_HEADERS });
  } catch (e) {
    return new Response(JSON.stringify({ error: 'Error' }), {
      status: 500,
      headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
    });
  }
}

/** 同意記録一覧を返す（管理者のみ）*/
async function handleGetConsents(request, env) {
  if (!await isAdmin(request, env)) {
    return new Response(JSON.stringify({ error: 'Unauthorized' }), {
      status: 401,
      headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
    });
  }

  const allKeys = [];
  let cursor = undefined;
  do {
    const result = await env.LOGIN_LOGS.list({ prefix: 'consent:', cursor });
    allKeys.push(...result.keys);
    cursor = result.list_complete ? undefined : result.cursor;
  } while (cursor);

  const entries = await Promise.all(
    allKeys.map(async k => {
      const val = await env.LOGIN_LOGS.get(k.name);
      try { return JSON.parse(val); } catch { return null; }
    })
  );
  const consents = entries.filter(Boolean).sort((a, b) => a.login.localeCompare(b.login));

  return new Response(JSON.stringify(consents), {
    headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
  });
}

/** ログイン画面HTML */
function loginPage() {
  const html = `<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Piscine Tracker - Login</title>
<style>
  :root {
    --bg: #0f0f14; --surface: #1a1a24; --border: #2a2a3a;
    --text: #e8e8f0; --text-dim: #8888a0;
    --accent: #6c5ce7; --gradient: linear-gradient(135deg, #6c5ce7, #00cec9);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
  }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 20px; padding: 48px 40px; text-align: center;
    max-width: 360px; width: 90%;
  }
  .logo { font-size: 48px; margin-bottom: 16px; }
  h1 {
    font-size: 22px; font-weight: 700; margin-bottom: 8px;
    background: var(--gradient);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
  }
  p { font-size: 13px; color: var(--text-dim); margin-bottom: 32px; line-height: 1.6; }
  .btn {
    display: inline-block; padding: 14px 32px;
    background: var(--gradient); color: white;
    border-radius: 12px; text-decoration: none;
    font-size: 15px; font-weight: 600;
    transition: opacity 0.2s; width: 100%; text-align: center;
  }
  .btn:hover { opacity: 0.85; }
  .note { margin-top: 20px; font-size: 11px; color: var(--text-dim); }
</style>
</head>
<body>
  <div class="card">
    <div class="logo">🏊</div>
    <h1>Piscine Tracker</h1>
    <p>42 Tokyo Piscine の在籍時間トラッカーです。<br>42 Intra アカウントでログインしてください。</p>
    <a href="/login" class="btn">42 Intra でログイン</a>
    <div class="note">42 Tokyo のアカウントが必要です</div>
  </div>
</body>
</html>`;
  return new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
}

/** OAuth 開始: state生成 → 42 Intra の認証ページへリダイレクト */
function startOAuth(env) {
  const state = crypto.randomUUID();
  const params = new URLSearchParams({
    client_id:     env.FORTY_TWO_CLIENT_ID,
    redirect_uri:  env.REDIRECT_URI,
    response_type: 'code',
    scope:         'public',
    state,
  });
  return new Response(null, {
    status: 302,
    headers: {
      'Location': `${AUTH_URL}?${params}`,
      // CSRF対策: stateをCookieに保存（workers.devドメイン内のみ）
      'Set-Cookie': `oauth_state=${state}; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax`,
    },
  });
}

/** OAuth コールバック処理 */
async function handleCallback(request, env, url) {
  const code  = url.searchParams.get('code');
  const state = url.searchParams.get('state');

  // CSRF チェック
  const cookieState = getCookie(request, 'oauth_state');
  if (!code || !state || state !== cookieState) {
    return errorPage('認証エラー', 'セキュリティチェックに失敗しました。もう一度ログインしてください。');
  }

  // Authorization Code → Access Token に交換
  const tokenResp = await fetch(TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      grant_type:    'authorization_code',
      client_id:     env.FORTY_TWO_CLIENT_ID,
      client_secret: env.FORTY_TWO_CLIENT_SECRET,
      code,
      redirect_uri:  env.REDIRECT_URI,
    }),
  });

  if (!tokenResp.ok) {
    const errText = await tokenResp.text().catch(() => '');
    return errorPage('認証失敗', `トークン取得に失敗しました。\n${errText}`);
  }

  const tokenData = await tokenResp.json();
  const access_token = tokenData.access_token;
  if (!access_token) {
    return errorPage('認証失敗', 'アクセストークンが取得できませんでした。');
  }

  // ユーザー情報取得
  const userResp = await fetch(USERINFO_URL, {
    headers: { 'Authorization': `Bearer ${access_token}` },
  });
  if (!userResp.ok) {
    return errorPage('認証失敗', 'ユーザー情報の取得に失敗しました。');
  }
  const user = await userResp.json();

  // Campus チェック: 42 Tokyo (id=26) のみ許可
  const campusIds   = (user.campus || []).map(c => c.id);
  const campusNames = (user.campus || []).map(c => c.name).join(', ') || '不明';
  if (!campusIds.includes(CAMPUS_ID_TOKYO)) {
    return errorPage(
      'アクセス拒否',
      `このサービスは 42 Tokyo の学生専用です。\nあなたのキャンパス: ${campusNames}`
    );
  }

  // 成功 → GitHub Pages の auth-callback.html にトークン＋ユーザー情報をハッシュで渡す
  // auth-callback.html が sessionStorage に保存してダッシュボードへ誘導する
  // ※ ブラウザから api.intra.42.fr を直接呼ぶと CORS 問題が起きるため、
  //    Worker 側で取得済みのユーザー情報をここで渡してキャッシュさせる
  // OAuth ログインをKVに記録
  try {
    const jst = new Date(Date.now() + 9 * 3600 * 1000);
    const ts  = jst.toISOString().replace('T', ' ').slice(0, 19) + ' JST';
    const ip  = request.headers.get('CF-Connecting-IP') || 'unknown';
    const ua  = request.headers.get('User-Agent') || '';
    const key = `log:${jst.toISOString().replace(/[^0-9]/g, '').slice(0, 14)}_${user.login}`;
    await env.LOGIN_LOGS.put(key, JSON.stringify({ login: user.login, method: 'oauth', ts, ip, ua })); // 無期限保持
  } catch {}

  const userMin = {
    login:  user.login,
    campus: user.campus,
    image:  user.image,
  };
  const userEncoded = encodeURIComponent(JSON.stringify(userMin));
  return new Response(null, {
    status: 302,
    headers: {
      'Location': `${GITHUB_PAGES_URL}/auth-callback.html#access_token=${access_token}&user=${userEncoded}`,
      'Set-Cookie': `oauth_state=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`,
    },
  });
}

/** Cookie 取得ヘルパー */
function getCookie(request, name) {
  const header = request.headers.get('Cookie') || '';
  const match = header.match(new RegExp(`(?:^|;\\s*)${name}=([^;]*)`));
  return match ? match[1] : null;
}

/** エラー画面 */
function errorPage(title, message) {
  const html = `<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${title}</title>
<style>
  body { font-family: system-ui; background: #0f0f14; color: #e8e8f0;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { background: #1a1a24; border: 1px solid #ff767530; border-radius: 20px;
          padding: 40px; text-align: center; max-width: 360px; width: 90%; }
  h1 { color: #ff7675; margin-bottom: 12px; font-size: 20px; }
  p { color: #8888a0; font-size: 13px; line-height: 1.6; white-space: pre-line; margin-bottom: 24px; }
  a { display: inline-block; padding: 12px 24px; background: #6c5ce7;
      color: white; border-radius: 10px; text-decoration: none; font-size: 14px; }
</style>
</head>
<body>
  <div class="card">
    <h1>⚠️ ${title}</h1>
    <p>${message}</p>
    <a href="/">トップへ戻る</a>
  </div>
</body>
</html>`;
  return new Response(html, {
    status: 403,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}
