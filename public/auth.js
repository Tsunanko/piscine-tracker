/**
 * 42 OAuth 認証モジュール
 * Piscine Tracker (GitHub Pages 用)
 *
 * 【このファイルの役割】
 * 全ページ（index.html, dashboard.html, stats.html 等）が
 * <script src="auth.js"> で読み込む共通認証ライブラリ。
 *
 * 【認証フロー（ざっくり）】
 *   1. login.html でログインボタンを押す
 *   2. Cloudflare Workers /login に飛ぶ（OAuth スタート）
 *   3. 42 Intra の認証ページでユーザーが許可する
 *   4. Workers がトークンを受け取り、42 Tokyo ユーザーかチェック
 *   5. auth-callback.html に #access_token=xxx を付けてリダイレクト
 *   6. auth-callback.html が sessionStorage にトークンを保存
 *   7. index.html 等に遷移 → checkAuth() でトークン確認
 *
 * 【なぜ Workers が必要か？】
 * GitHub Pages は静的ファイルしか配信できない。
 * OAuth の "code → token 交換" はサーバー側で行う必要がある
 * （CLIENT_SECRET をブラウザに公開してはいけないため）。
 * そこで Cloudflare Workers がサーバー役を担う。
 */

// ─── sessionStorage のキー名 ─────────────────────────────────────────────
// sessionStorage: ブラウザのタブを閉じると自動削除される一時保存領域
// （localStorageはタブをまたいで保持されるためセキュリティ上避ける）
const AUTH_TOKEN_KEY = 'piscine_42_token';  // アクセストークン保存用
const AUTH_USER_KEY  = 'piscine_42_user';   // ユーザー情報キャッシュ用

// ─── OAuth 設定 ───────────────────────────────────────────────────────────
// CLIENT_ID: 42 Intra の OAuth App の公開識別子（秘密ではない）
// REDIRECT_URI: 認証成功後に戻ってくるURL（42 IntraアプリのリダイレクトURIと一致させる必要あり）
const CLIENT_ID    = 'u-s4t2ud-22e19b2f4cbb1a09c37b335356f28dcaceeb620b819901480a6c7a6f62d67fc9';
const REDIRECT_URI = 'https://tsunanko.github.io/piscine-tracker/auth-callback.html';
const CAMPUS_ID    = 26;  // 42 Tokyo のキャンパスID（他キャンパスはアクセス拒否）

// ─── OAuth 認可URL（参考・現在は Workers 経由なので直接は使わない）──────
// response_type=token: Implicit Flow（旧方式）の場合の設定
// 現在は Authorization Code Flow（Workers経由）を使用
const OAUTH_URL =
  `https://api.intra.42.fr/oauth/authorize` +
  `?client_id=${CLIENT_ID}` +
  `&redirect_uri=${encodeURIComponent(REDIRECT_URI)}` +
  `&response_type=token` +   // ← Implicit Flow 用（現在は Workers 経由に変更済み）
  `&scope=public`;            // ← public スコープ = 基本的なユーザー情報の読み取り

// ─── トークン操作（sessionStorage のラッパー）────────────────────────────
// これらの関数で sessionStorage に直接アクセスするコードを一箇所にまとめる
// （将来的に保存先を変えたいときにここだけ修正すればよい）
function getToken()        { return sessionStorage.getItem(AUTH_TOKEN_KEY); }
function setToken(token)   { sessionStorage.setItem(AUTH_TOKEN_KEY, token); }
function clearSession()    {
  sessionStorage.removeItem(AUTH_TOKEN_KEY);  // トークンを削除
  sessionStorage.removeItem(AUTH_USER_KEY);   // ユーザーキャッシュも削除
}

// ─── 42 API /v2/me でトークン検証 ────────────────────────────────────────
// /v2/me: 現在のトークンに紐づくユーザーの情報を返す42 APIのエンドポイント
// トークンが無効・期限切れの場合は 401 が返るので null を返す
async function fetchMe(token) {
  try {
    const res = await fetch('https://api.intra.42.fr/v2/me', {
      headers: { Authorization: `Bearer ${token}` },  // Bearer認証
    });
    if (!res.ok) return null;  // 401 や 403 の場合は null
    return await res.json();   // { login, campus, image, ... } を返す
  } catch {
    // ネットワークエラーなど例外時も null を返す（エラーを上に伝播しない）
    return null;
  }
}

// ─── 認証チェック（キャッシュ付き）──────────────────────────────────────
// 【設計ポイント】
// ページ遷移のたびに毎回 42 API を呼ぶのは遅い & API消費が増える。
// sessionStorage にユーザー情報をキャッシュしておくことで
// タブを開いている間は1回だけ API を呼べばよい。
async function checkAuth() {
  const token = getToken();
  if (!token) return null;  // トークンなし = 未ログイン

  // キャッシュ確認: sessionStorage にユーザー情報があれば API を呼ばず再利用
  const cached = sessionStorage.getItem(AUTH_USER_KEY);
  if (cached) {
    try { return JSON.parse(cached); } catch {}
    // JSON.parse が失敗した場合（壊れたデータ）は素通りして API を呼び直す
  }

  // 42 API でトークン検証 + ユーザー情報取得
  const user = await fetchMe(token);
  if (!user) {
    // トークンが無効 or 期限切れ → セッションをクリアして null を返す
    clearSession();
    return null;
  }

  // ユーザー情報をキャッシュに保存（次のページ遷移では API を呼ばない）
  sessionStorage.setItem(AUTH_USER_KEY, JSON.stringify(user));
  return user;
}

// ─── 認証必須ページ用ガード ──────────────────────────────────────────────
// 使い方: 認証が必要なページの先頭で await requireAuth() を呼ぶ
// ログインしていなければ login.html にリダイレクト
// ログイン済みなら右上にユーザーバーを表示してユーザーオブジェクトを返す
async function requireAuth() {
  const user = await checkAuth();
  if (!user) {
    // window.location.replace: ブラウザの「戻る」でこのページに戻れないようにする
    // （replace: 履歴に追加しない / href: 履歴に追加する）
    window.location.replace('login.html');
    return null;
  }
  injectUserBar(user);  // 右上にアバターとユーザー名を表示
  return user;
}

// ─── ログイン開始 ─────────────────────────────────────────────────────────
// Cloudflare Workers の /login エンドポイントへリダイレクトする
// Workers が 42 Intra の OAuth フローを開始し、最終的に access_token を返す
//
// なぜ Workers 経由か？
// Authorization Code Flow ではサーバー側で "code → token 交換" が必要。
// CLIENT_SECRET をブラウザに持たせることはセキュリティ上 NG なため、
// Workers がサーバー役を担う。
function loginWith42() {
  window.location.href = 'https://piscine-tracker.tsunanko.workers.dev/login';
}

// ─── ログアウト ───────────────────────────────────────────────────────────
// sessionStorage を削除してログインページへ遷移
// （サーバー側のセッション削除は不要 = トークンは sessionStorage のみで管理）
function logout() {
  clearSession();
  window.location.replace('login.html');
}

// ─── ユーザーバー注入（右上に固定表示） ──────────────────────────────────
// ログイン後の全ページ右上に「アバター + ログイン名 + キャンパス名 + ログアウトボタン」
// を表示する。requireAuth() から自動で呼ばれる。
//
// 【実装メモ】
// - document.getElementById で既存チェック（2重挿入防止）
// - document.createElement で DOM要素を作成（innerHTML の XSS リスクを最小化）
// - avatarSrc がない場合はイニシャル表示にフォールバック
function injectUserBar(user) {
  // 既にバーが表示されている場合は何もしない（ページ内で2回呼ばれた場合など）
  if (document.getElementById('auth-user-bar')) return;

  // 42 Tokyo キャンパス情報を取得（user.campus は配列）
  const campus = (user.campus || []).find(c => c.id === CAMPUS_ID);
  const campusName = campus ? campus.name : (user.campus?.[0]?.name || '42');

  const bar = document.createElement('div');
  bar.id = 'auth-user-bar';
  // cssText: スタイルを文字列で一括設定（インラインスタイル）
  // position:fixed + top/right でスクロールしても常に右上に表示
  bar.style.cssText = [
    'position:fixed', 'top:10px', 'right:12px', 'z-index:9999',
    'display:flex', 'align-items:center', 'gap:8px',
    'background:#1a1a24', 'border:1px solid #2a2a3a',
    'border-radius:20px', 'padding:5px 12px 5px 6px',
    'font-size:12px', 'color:#c8c8d8',
    'box-shadow:0 2px 8px rgba(0,0,0,0.4)',
  ].join(';');

  // アバター画像URL: user.image?.versions?.small（小サイズ）を優先
  // ?.（オプショナルチェーン）: null/undefined の場合でもエラーにならない
  const avatarSrc = user.image?.versions?.small || user.image?.link || '';
  bar.innerHTML = `
    ${avatarSrc
      // 画像ありの場合: <img>タグで表示
      ? `<img src="${avatarSrc}" style="width:22px;height:22px;border-radius:50%;object-fit:cover;">`
      // 画像なしの場合: login名の最初の文字（大文字）をイニシャルとして表示
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
  // <body> の最後に追加（z-index:9999 で他の要素の上に表示される）
  document.body.appendChild(bar);
}
