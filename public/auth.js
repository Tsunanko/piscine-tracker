/**
 * 42 OAuth 認証モジュール
 * Piscine Tracker (GitHub Pages 用)
 *
 * 認証フロー:
 *   login.html → Cloudflare Workers /login
 *   → 42 OAuth (Authorization Code Flow)
 *   → Workers /auth/callback (campus確認)
 *   → auth-callback.html#access_token=xxx
 *   → sessionStorage保存 → dashboard.html
 */

const AUTH_TOKEN_KEY = 'piscine_42_token';
const AUTH_USER_KEY  = 'piscine_42_user';

const CLIENT_ID    = 'u-s4t2ud-22e19b2f4cbb1a09c37b335356f28dcaceeb620b819901480a6c7a6f62d67fc9';
const REDIRECT_URI = 'https://tsunanko.github.io/piscine-tracker/auth-callback.html';
const CAMPUS_ID    = 26;  // 42 Tokyo

const OAUTH_URL =
  `https://api.intra.42.fr/oauth/authorize` +
  `?client_id=${CLIENT_ID}` +
  `&redirect_uri=${encodeURIComponent(REDIRECT_URI)}` +
  `&response_type=token` +
  `&scope=public`;

// ─── トークン操作 ────────────────────────────
function getToken()        { return sessionStorage.getItem(AUTH_TOKEN_KEY); }
function setToken(token)   { sessionStorage.setItem(AUTH_TOKEN_KEY, token); }
function clearSession()    {
  sessionStorage.removeItem(AUTH_TOKEN_KEY);
  sessionStorage.removeItem(AUTH_USER_KEY);
}

// ─── 42 API /v2/me でトークン検証 ────────────
async function fetchMe(token) {
  try {
    const res = await fetch('https://api.intra.42.fr/v2/me', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

// ─── 認証チェック（キャッシュ付き）──────────
async function checkAuth() {
  const token = getToken();
  if (!token) return null;

  // sessionStorage にユーザーキャッシュがあれば再利用
  const cached = sessionStorage.getItem(AUTH_USER_KEY);
  if (cached) {
    try { return JSON.parse(cached); } catch {}
  }

  const user = await fetchMe(token);
  if (!user) { clearSession(); return null; }

  sessionStorage.setItem(AUTH_USER_KEY, JSON.stringify(user));
  return user;
}

// ─── 認証必須（未ログインならログインページへ）──
async function requireAuth() {
  const user = await checkAuth();
  if (!user) {
    window.location.replace('login.html');
    return null;
  }
  injectUserBar(user);
  return user;
}

// ─── ログイン ─────────────────────────────────
// Cloudflare Workers経由でAuthorization Code Flowを開始
// （42はImplicit Flowを非対応のため Workers でトークン交換を行う）
function loginWith42() {
  window.location.href = 'https://piscine-tracker.tsunanko.workers.dev/login';
}

// ─── ログアウト ───────────────────────────────
function logout() {
  clearSession();
  window.location.replace('login.html');
}

// ─── ユーザーバー注入（右上に固定表示）────────
function injectUserBar(user) {
  if (document.getElementById('auth-user-bar')) return;

  const campus = (user.campus || []).find(c => c.id === CAMPUS_ID);
  const campusName = campus ? campus.name : (user.campus?.[0]?.name || '42');

  const bar = document.createElement('div');
  bar.id = 'auth-user-bar';
  bar.style.cssText = [
    'position:fixed', 'top:10px', 'right:12px', 'z-index:9999',
    'display:flex', 'align-items:center', 'gap:8px',
    'background:#1a1a24', 'border:1px solid #2a2a3a',
    'border-radius:20px', 'padding:5px 12px 5px 6px',
    'font-size:12px', 'color:#c8c8d8',
    'box-shadow:0 2px 8px rgba(0,0,0,0.4)',
  ].join(';');

  const avatarSrc = user.image?.versions?.small || user.image?.link || '';
  bar.innerHTML = `
    ${avatarSrc
      ? `<img src="${avatarSrc}" style="width:22px;height:22px;border-radius:50%;object-fit:cover;">`
      : `<span style="width:22px;height:22px;border-radius:50%;background:#6c5ce7;display:flex;align-items:center;justify-content:center;font-size:11px;color:#fff">${user.login[0].toUpperCase()}</span>`
    }
    <span style="color:#e8e8f0;font-weight:600">${user.login}</span>
    <span style="color:#555570;font-size:10px">${campusName}</span>
    <button onclick="logout()" style="
      background:none;border:1px solid #2a2a3a;color:#8888a0;
      border-radius:10px;padding:2px 8px;font-size:10px;cursor:pointer;
      margin-left:4px;transition:all 0.15s;
    " onmouseover="this.style.borderColor='#ff7675';this.style.color='#ff7675'"
       onmouseout="this.style.borderColor='#2a2a3a';this.style.color='#8888a0'"
    >ログアウト</button>
  `;
  document.body.appendChild(bar);
}
