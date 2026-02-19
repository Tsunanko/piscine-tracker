/**
 * Cloudflare Workers - 42 Intra OAuth認証プロキシ
 *
 * 動作：
 * 1. 未ログイン → ログイン画面を表示
 * 2. 42 Intra でログイン → campus_id が 42 Tokyo (26) かチェック
 * 3. OK なら GitHub Pages の dashboard.html にリダイレクト
 * 4. NG (他キャンパス) → アクセス拒否画面
 * 5. セッションは Cookie で24時間維持
 */

const GITHUB_PAGES_URL = 'https://tsunanko.github.io/piscine-tracker';
const CAMPUS_ID_TOKYO = 26;
const SESSION_TTL_SEC = 60 * 60 * 24; // 24時間

// 42 Intra OAuth エンドポイント
const AUTH_URL     = 'https://api.intra.42.fr/oauth/authorize';
const TOKEN_URL    = 'https://api.intra.42.fr/oauth/token';
const USERINFO_URL = 'https://api.intra.42.fr/v2/me';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // --- /auth/callback: 42からのコールバック ---
    if (url.pathname === '/auth/callback') {
      return handleCallback(request, env, url);
    }

    // --- /logout ---
    if (url.pathname === '/logout') {
      return new Response(null, {
        status: 302,
        headers: {
          'Location': '/',
          'Set-Cookie': `session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`,
        },
      });
    }

    // --- セッション確認 ---
    const session = await getSession(request, env);
    if (session) {
      // ログイン済み → GitHub Pages へリダイレクト（またはプロキシ）
      const target = url.searchParams.get('redirect') || '/dashboard.html';
      return Response.redirect(`${GITHUB_PAGES_URL}${target}`, 302);
    }

    // --- 未ログイン → ログイン画面 ---
    if (url.pathname === '/login') {
      return startOAuth(env, url);
    }

    // --- ルート (/) → ログイン画面 ---
    return loginPage();
  }
};

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
  .logo {
    font-size: 48px; margin-bottom: 16px;
  }
  h1 {
    font-size: 22px; font-weight: 700; margin-bottom: 8px;
    background: var(--gradient);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
  }
  p {
    font-size: 13px; color: var(--text-dim); margin-bottom: 32px; line-height: 1.6;
  }
  .btn {
    display: inline-block; padding: 14px 32px;
    background: var(--gradient); color: white;
    border-radius: 12px; text-decoration: none;
    font-size: 15px; font-weight: 600;
    transition: opacity 0.2s; border: none; cursor: pointer; width: 100%;
  }
  .btn:hover { opacity: 0.85; }
  .note {
    margin-top: 20px; font-size: 11px; color: var(--text-dim);
  }
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

/** OAuth 開始: 42 Intra の認証ページへリダイレクト */
function startOAuth(env, url) {
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
      // state を Cookie に保存（CSRF対策）
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
    return errorPage('認証エラー', 'Invalid state parameter. Please try again.');
  }

  // トークン取得
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
    return errorPage('認証失敗', 'トークン取得に失敗しました。');
  }
  const { access_token } = await tokenResp.json();

  // ユーザー情報取得
  const userResp = await fetch(USERINFO_URL, {
    headers: { 'Authorization': `Bearer ${access_token}` },
  });
  if (!userResp.ok) {
    return errorPage('認証失敗', 'ユーザー情報の取得に失敗しました。');
  }
  const user = await userResp.json();

  // Campus チェック: 42 Tokyo (26) のみ許可
  const campusIds = (user.campus || []).map(c => c.id);
  if (!campusIds.includes(CAMPUS_ID_TOKYO)) {
    return errorPage(
      'アクセス拒否',
      `このサービスは 42 Tokyo の学生専用です。\n(あなたのキャンパス: ${(user.campus || []).map(c => c.name).join(', ') || '不明'})`
    );
  }

  // セッション作成 (KVに保存)
  const sessionId = crypto.randomUUID();
  const sessionData = {
    login:      user.login,
    campus_ids: campusIds,
    created_at: Date.now(),
  };
  await env.SESSIONS.put(sessionId, JSON.stringify(sessionData), {
    expirationTtl: SESSION_TTL_SEC,
  });

  // ダッシュボードへリダイレクト
  return new Response(null, {
    status: 302,
    headers: {
      'Location': `${GITHUB_PAGES_URL}/dashboard.html`,
      'Set-Cookie': [
        `session=${sessionId}; Path=/; Max-Age=${SESSION_TTL_SEC}; HttpOnly; Secure; SameSite=Lax`,
        `oauth_state=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`,
      ].join(', '),
    },
  });
}

/** セッション確認 */
async function getSession(request, env) {
  const sessionId = getCookie(request, 'session');
  if (!sessionId) return null;
  const data = await env.SESSIONS.get(sessionId);
  if (!data) return null;
  return JSON.parse(data);
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
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }
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
