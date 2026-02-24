/**
 * Cloudflare Workers - 42 Intra OAuth認証プロキシ
 *
 * 【このファイルの役割】
 * GitHub Pages（静的サイト）では「サーバー処理」が一切できない。
 * OAuth の "Authorization Code Flow" はサーバーが必要なので、
 * Cloudflare Workers がその役割を担う。
 *
 * 【Cloudflare Workers とは？】
 * - Cloudflare のエッジサーバーで動くサーバーレス JavaScript 環境
 * - Node.js ではなく Web Workers API に準拠（fetch, crypto, URL 等が使える）
 * - デプロイ: wrangler deploy コマンド1つで完了
 * - 無料プラン: 10万リクエスト/日まで
 * - URL: https://piscine-tracker.tsunanko.workers.dev
 *
 * 【エンドポイント一覧】
 * GET  /                  → ログイン画面 HTML を返す
 * GET  /login             → 42 OAuth フローを開始（認証ページへリダイレクト）
 * GET  /auth/callback     → OAuth コールバック（code → token 交換 + campus チェック）
 * POST /api/log           → ログイン記録を Cloudflare KV に保存
 * POST /api/consent       → 利用規約同意を KV に保存
 * POST /api/simple-login  → 合言葉認証（Piscine生向け）
 * GET  /api/logs          → ログイン記録一覧（管理者のみ）
 * GET  /api/consents      → 同意記録一覧（管理者のみ）
 *
 * 【Cloudflare KV とは？】
 * - キーバリューストア（Redisのようなもの）
 * - Workers から env.LOGIN_LOGS.get(key) / .put(key, value) で操作
 * - ログイン記録・同意記録を永続保存するために使用
 * - key の命名: "log:YYYYMMDDHHMMSS_login" でソート可能にする
 *
 * 【Secrets（環境変数）一覧 - wrangler secret put で登録】
 * FORTY_TWO_CLIENT_ID     → 42 Intra OAuth App の UID
 * FORTY_TWO_CLIENT_SECRET → 42 Intra OAuth App の Secret（絶対に公開しない）
 * REDIRECT_URI            → コールバックURL（このWorkerの /auth/callback）
 * ADMIN_SECRET            → 管理者認証用の任意文字列
 * PASS_HASH_1             → 合言葉1のSHA-256ハッシュ（Piscine生向け合言葉認証）
 * PASS_HASH_2             → 合言葉2のSHA-256ハッシュ（予備）
 */

// ─── 定数 ─────────────────────────────────────────────────────────────────
const GITHUB_PAGES_URL = 'https://tsunanko.github.io/piscine-tracker';
const CAMPUS_ID_TOKYO  = 26;  // 42 Tokyo のキャンパスID（他キャンパスはアクセス拒否）

// 42 Intra の OAuth エンドポイント
const AUTH_URL     = 'https://api.intra.42.fr/oauth/authorize';  // 認可ページ
const TOKEN_URL    = 'https://api.intra.42.fr/oauth/token';       // token交換
const USERINFO_URL = 'https://api.intra.42.fr/v2/me';            // ユーザー情報

// CORS（Cross-Origin Resource Sharing）設定
// GitHub Pages (tsunanko.github.io) からのリクエストのみ許可
// OPTIONS リクエスト（ブラウザのプリフライト）に対してこのヘッダーを返す
const CORS_HEADERS = {
  'Access-Control-Allow-Origin': 'https://tsunanko.github.io',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, X-Admin-Secret, Authorization',
};

/**
 * 管理者権限チェック（2つの認証方式をサポート）
 *
 * 方式1: X-Admin-Secret ヘッダー
 *   - curl や REST クライアントから直接叩くときに使う
 *   - 例: curl -H "X-Admin-Secret: xxxx" https://...workers.dev/api/logs
 *   - ADMIN_SECRET は wrangler secret put ADMIN_SECRET で登録する
 *
 * 方式2: Authorization: Bearer <token>
 *   - ブラウザからのリクエスト（admin.html が使用）
 *   - 42 access token: 42 OAuth でログインした場合（42 API で login名を検証）
 *
 * @param {Request} request - Fetch API の Request オブジェクト
 * @param {Object} env - Workers の環境変数・KV バインディング
 * @returns {boolean} 管理者なら true
 */
async function isAdmin(request, env) {
  // 方式1: X-Admin-Secret ヘッダーによる認証（curl 向け）
  const secret = request.headers.get('X-Admin-Secret');
  if (env.ADMIN_SECRET && secret === env.ADMIN_SECRET) return true;

  // 方式2: Authorization: Bearer による認証（ブラウザ向け）
  const auth = request.headers.get('Authorization');
  if (!auth) return false;
  // "Bearer xxxx" から "xxxx" を取り出す
  const token = auth.replace(/^Bearer\s+/, '');

  // 42 OAuth トークンの場合: 42 API /v2/me で実際にユーザーを確認
  try {
    const res = await fetch(USERINFO_URL, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.ok) {
      const user = await res.json();
      return user.login === 'ijoja';  // 管理者の login 名で照合
    }
  } catch {}
  return false;
}

/**
 * Cloudflare Workers のメインエントリーポイント
 *
 * Workers は「リクエストが来るたびに fetch() が呼ばれる」サーバーレス関数。
 * 従来の Express.js サーバーと違い、常時起動ではなくリクエスト駆動。
 * URL パスを見て適切なハンドラー関数に振り分ける（ルーティング）。
 *
 * export default: ES Modules 形式（wrangler.toml の main = "index.js" で指定）
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);  // URL を解析（pathname, searchParams 等が使える）

    // ─── CORS プリフライト対応 ─────────────────────────────────────────
    // ブラウザは異なるオリジン（ドメイン）へのリクエスト前に
    // OPTIONS メソッドで「このリクエストは許可されますか？」と確認する。
    // これを「プリフライトリクエスト」と呼ぶ。
    // CORS_HEADERS を返すことで「OK、許可します」と伝える。
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    // ─── ルーティング ─────────────────────────────────────────────────
    // pathname でどの処理をするか分岐する（Express の app.get('/path', ...) に相当）

    if (url.pathname === '/auth/callback') {
      // OAuth コールバック: 42 Intra から code を受け取り token に交換
      return handleCallback(request, env, url);
    }

    if (url.pathname === '/login') {
      // OAuth 開始: 42 Intra の認証ページへリダイレクト
      return startOAuth(env);
    }

    // ─── 合言葉認証 API ───────────────────────────────────────────────
    // Piscine生向けの合言葉（共有パスワード）によるログイン
    // クライアントがSHA-256でハッシュ化したパスフレーズをサーバーで照合
    // 管理者権限は付与しない（OAuthログインのみが管理者になれる）
    if (url.pathname === '/api/simple-login' && request.method === 'POST') {
      return handleSimpleLogin(request, env);
    }

    // ─── ログイン記録 API ──────────────────────────────────────────────
    // GitHub Pages の各ページがログイン成功時に呼び出す
    // KV に { login, method, timestamp, ip, ua } を保存
    if (url.pathname === '/api/log' && request.method === 'POST') {
      return handleLog(request, env);
    }

    // ─── 同意記録 API ─────────────────────────────────────────────────
    // 利用規約への同意を KV に保存（初回のみ、上書きしない）
    if (url.pathname === '/api/consent' && request.method === 'POST') {
      return handleConsent(request, env);
    }

    // ─── ログ閲覧 API（管理者のみ）────────────────────────────────────
    // admin.html が呼び出す。isAdmin() で認証チェックしてから KV のデータを返す
    if (url.pathname === '/api/logs' && request.method === 'GET') {
      return handleGetLogs(request, env);
    }

    // ─── 同意記録一覧 API（管理者のみ）────────────────────────────────
    if (url.pathname === '/api/consents' && request.method === 'GET') {
      return handleGetConsents(request, env);
    }

    // ─── デフォルト: ルート (/) → ログイン画面 ────────────────────────
    return loginPage();
  }
};

/**
 * ログイン記録を Cloudflare KV に保存する
 *
 * GitHub Pages のログイン成功時（auth-callback.html）から呼ばれる。
 * KV に保存することで後から管理者が閲覧できる。
 *
 * KV Key の設計: "log:YYYYMMDDHHMMSS_login"
 * → 時系列でソート可能（アルファベット順 = 時系列順になる）
 * → 例: "log:20260224103045_ijoja"
 *
 * CF-Connecting-IP: Cloudflare が付加するヘッダー
 * → 実際のクライアントIPアドレス（プロキシ越しでも正しいIPが取れる）
 */
async function handleLog(request, env) {
  try {
    const body = await request.json();
    const { login, method } = body;
    if (!login || !method) {
      return new Response('Bad Request', { status: 400, headers: CORS_HEADERS });
    }

    // JST（日本標準時）でタイムスタンプを作成
    // new Date(Date.now() + 9 * 3600 * 1000): UTC + 9時間 = JST
    const jst = new Date(Date.now() + 9 * 3600 * 1000);
    const ts  = jst.toISOString().replace('T', ' ').slice(0, 19) + ' JST';
    const ip  = request.headers.get('CF-Connecting-IP') || 'unknown';
    const ua  = request.headers.get('User-Agent') || '';  // ブラウザの種類など

    const entry = { login, method, ts, ip, ua };
    // KV key: "log:YYYYMMDDHHMMSS_login"（時系列ソート可能な形式）
    const key = `log:${jst.toISOString().replace(/[^0-9]/g, '').slice(0, 14)}_${login}`;
    await env.LOGIN_LOGS.put(key, JSON.stringify(entry)); // TTL なし = 無期限保持

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

/** 同意記録を保存（初回のみ記録・上書きしない）*/
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
    // piscine:login トークンの場合、login と一致するか確認
    const auth = request.headers.get('Authorization') || '';
    const bearerToken = auth.startsWith('Bearer ') ? auth.slice(7) : '';
    if (bearerToken.startsWith('piscine:')) {
      const tokenLogin = bearerToken.slice(8); // 'piscine:'.length
      if (tokenLogin !== login) {
        return new Response(JSON.stringify({ error: 'Unauthorized' }), {
          status: 401,
          headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
        });
      }
    }
    // 既に記録済みなら上書きしない（初回の同意日時を保持）
    const existing = await env.LOGIN_LOGS.get(`consent:${login}`);
    if (existing) {
      return new Response('OK', { status: 200, headers: CORS_HEADERS });
    }
    const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
    const entry = { login, consentedAt: consentedAt || new Date().toISOString(), method: method || 'unknown', ip };
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

/**
 * 合言葉認証（Piscine生向けシンプルログイン）
 *
 * 【フロー】
 * 1. クライアントがログイン名 + SHA-256ハッシュ化した合言葉を POST
 * 2. サーバー（Workers）が env.PASS_HASH_1 / PASS_HASH_2 と照合
 * 3. 一致すればログイン記録を KV に保存して { ok: true } を返す
 * 4. クライアントが sessionStorage に 'piscine:${login}' トークンを保存
 *
 * 【セキュリティ注意事項】
 * - piscine: プレフィックストークンには管理者権限を一切付与しない（isAdmin参照）
 * - 合言葉は共有シークレットのため、なりすましリスクは残る（閲覧専用のため許容）
 * - PASS_HASH は wrangler secret put PASS_HASH_1 で登録（ソースコードに書かない）
 */
async function handleSimpleLogin(request, env) {
  try {
    const body = await request.json();
    const { login, passHash } = body;

    // バリデーション: login と passHash が必須
    if (!login || typeof login !== 'string' || !passHash || typeof passHash !== 'string') {
      return new Response(JSON.stringify({ error: 'Bad Request' }), {
        status: 400,
        headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
      });
    }

    // ログイン名のサニタイズ（英数字・ハイフン・アンダースコアのみ許可）
    if (!/^[a-zA-Z0-9_-]{1,30}$/.test(login)) {
      return new Response(JSON.stringify({ error: 'Invalid login format' }), {
        status: 400,
        headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
      });
    }

    // 合言葉ハッシュの照合（PASS_HASH_1 または PASS_HASH_2 と一致すれば OK）
    const validHashes = [env.PASS_HASH_1, env.PASS_HASH_2].filter(Boolean);
    if (validHashes.length === 0) {
      // Secrets が設定されていない場合は機能無効
      return new Response(JSON.stringify({ error: 'Simple login not configured' }), {
        status: 503,
        headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
      });
    }
    if (!validHashes.includes(passHash)) {
      return new Response(JSON.stringify({ error: 'Invalid password' }), {
        status: 401,
        headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
      });
    }

    // ログイン記録を KV に保存
    try {
      const jst = new Date(Date.now() + 9 * 3600 * 1000);
      const ts  = jst.toISOString().replace('T', ' ').slice(0, 19) + ' JST';
      const ip  = request.headers.get('CF-Connecting-IP') || 'unknown';
      const ua  = request.headers.get('User-Agent') || '';
      const key = `log:${jst.toISOString().replace(/[^0-9]/g, '').slice(0, 14)}_${login}`;
      await env.LOGIN_LOGS.put(key, JSON.stringify({ login, method: 'passphrase', ts, ip, ua }));
    } catch {} // ログ失敗でもログイン自体は続行

    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: 'Internal Server Error' }), {
      status: 500,
      headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
    });
  }
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

/**
 * OAuth フロー開始: state 生成 → 42 Intra の認証ページへリダイレクト
 *
 * 【Authorization Code Flow の仕組み】
 * 1. Workers が state（乱数）を生成して Cookie に保存
 * 2. 42 Intra の認証ページへリダイレクト（state を URL に含める）
 * 3. ユーザーが許可すると、42 が Workers の /auth/callback?code=xxx&state=xxx へリダイレクト
 * 4. Workers が Cookie の state と URL の state を照合（CSRF 対策）
 * 5. code を access_token に交換
 *
 * 【CSRF（Cross-Site Request Forgery）対策とは？】
 * 悪意あるサイトが被害者のブラウザ経由でリクエストを送る攻撃。
 * state パラメータを使って「自分が開始したフローか」を確認する。
 * Cookie: HttpOnly（JSからアクセス不可）+ SameSite=Lax（外部サイトからの送信を制限）
 */
function startOAuth(env) {
  const state = crypto.randomUUID();  // ランダムなUUID（CSRF防止用の乱数）
  const params = new URLSearchParams({
    client_id:     env.FORTY_TWO_CLIENT_ID,
    redirect_uri:  env.REDIRECT_URI,    // Workers の /auth/callback
    response_type: 'code',              // Authorization Code Flow を使用
    scope:         'public',            // 読み取りのみ（書き込み権限は要求しない）
    state,                              // CSRF 対策用の乱数
  });
  return new Response(null, {
    status: 302,  // 302 Found: リダイレクト
    headers: {
      'Location': `${AUTH_URL}?${params}`,
      // CSRF 対策: state を Cookie に保存（10分間有効）
      // HttpOnly: JavaScript から Cookie を読めない（XSS 対策）
      // Secure: HTTPS のみで送信
      // SameSite=Lax: 外部サイトからの GET リダイレクトは許可、POST は拒否
      'Set-Cookie': `oauth_state=${state}; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax`,
    },
  });
}

/**
 * OAuth コールバック処理
 * 42 Intra から code を受け取り、access_token に交換してクライアントに渡す
 *
 * このエンドポイントが OAuth の「核心部分」。
 * CLIENT_SECRET を使うため、ブラウザではなくサーバー（Workers）が処理する必要がある。
 */
async function handleCallback(request, env, url) {
  const code  = url.searchParams.get('code');   // 42 Intra が発行した一時コード
  const state = url.searchParams.get('state');  // CSRF チェック用

  // ─── CSRF チェック ──────────────────────────────────────────────────
  // Cookie に保存した state と URL の state が一致するか確認
  // 一致しない = 第三者が不正にこの URL を開いた可能性がある
  const cookieState = getCookie(request, 'oauth_state');
  if (!code || !state || state !== cookieState) {
    return errorPage('認証エラー', 'セキュリティチェックに失敗しました。もう一度ログインしてください。');
  }

  // ─── Authorization Code → Access Token に交換 ───────────────────────
  // この POST リクエストで CLIENT_SECRET を使う（ブラウザには渡さない）
  const tokenResp = await fetch(TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      grant_type:    'authorization_code',         // フロー種別
      client_id:     env.FORTY_TWO_CLIENT_ID,
      client_secret: env.FORTY_TWO_CLIENT_SECRET,  // 秘密鍵（Workers Secretから取得）
      code,                                        // 42 Intra が発行した一時コード
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

  // ─── ユーザー情報取得（campus_id チェックのため）──────────────────
  const userResp = await fetch(USERINFO_URL, {
    headers: { 'Authorization': `Bearer ${access_token}` },
  });
  if (!userResp.ok) {
    return errorPage('認証失敗', 'ユーザー情報の取得に失敗しました。');
  }
  const user = await userResp.json();

  // ─── Campus チェック: 42 Tokyo (id=26) のみ許可 ─────────────────────
  // user.campus は配列（複数キャンパスに所属できるため）
  const campusIds   = (user.campus || []).map(c => c.id);
  const campusNames = (user.campus || []).map(c => c.name).join(', ') || '不明';
  if (!campusIds.includes(CAMPUS_ID_TOKYO)) {
    return errorPage(
      'アクセス拒否',
      `このサービスは 42 Tokyo の学生専用です。\nあなたのキャンパス: ${campusNames}`
    );
  }

  // ─── ログイン記録を KV に保存 ────────────────────────────────────────
  // エラーが起きてもログイン処理自体は続行するため try/catch で囲む
  try {
    const jst = new Date(Date.now() + 9 * 3600 * 1000);
    const ts  = jst.toISOString().replace('T', ' ').slice(0, 19) + ' JST';
    const ip  = request.headers.get('CF-Connecting-IP') || 'unknown';
    const ua  = request.headers.get('User-Agent') || '';
    const key = `log:${jst.toISOString().replace(/[^0-9]/g, '').slice(0, 14)}_${user.login}`;
    await env.LOGIN_LOGS.put(key, JSON.stringify({ login: user.login, method: 'oauth', ts, ip, ua }));
  } catch {}

  // ─── 成功: GitHub Pages の auth-callback.html へリダイレクト ────────
  // URLのハッシュ（#以降）に access_token と user情報を埋め込む
  //
  // 【なぜ URL ハッシュを使うのか？】
  // - ハッシュは HTTP サーバーに送信されない（ブラウザ内だけで処理）
  // - GitHub Pages のサーバーログにトークンが残らない
  // - auth-callback.html が window.location.hash から取り出して sessionStorage に保存
  //
  // 【ユーザー情報も一緒に渡す理由】
  // Workers側で取得済みの user情報をクライアントに渡すことで、
  // ブラウザが再度 42 API /v2/me を呼ぶ必要をなくせる（高速化）
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
      // Cookie をクリア（state の有効期限を 0 に設定 = 即時削除）
      'Set-Cookie': `oauth_state=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`,
    },
  });
}

/**
 * Cookie から指定した名前の値を取得するヘルパー関数
 *
 * Cookie ヘッダーの形式: "name1=value1; name2=value2; name3=value3"
 * 正規表現でパースして指定した name の value を返す。
 *
 * @param {Request} request - Fetch API の Request オブジェクト
 * @param {string} name - 取得したい Cookie の名前
 * @returns {string|null} Cookie の値、存在しない場合は null
 */
function getCookie(request, name) {
  const header = request.headers.get('Cookie') || '';
  // 正規表現: "name=value" の value 部分を取り出す
  // (?:^|;\s*) → 文字列の先頭か "; " の後
  // ([^;]*) → ";" 以外の文字（= Cookie の値）
  const match = header.match(new RegExp(`(?:^|;\\s*)${name}=([^;]*)`));
  return match ? match[1] : null;
}

/**
 * エラー画面 HTML を返す
 *
 * @param {string} title - エラーのタイトル（例: "アクセス拒否"）
 * @param {string} message - エラーの詳細メッセージ
 * @returns {Response} 403 ステータスの HTML レスポンス
 */
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

