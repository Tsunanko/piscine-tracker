# Cloudflare Workers セットアップ手順

## 事前確認
- 42 Intra OAuth アプリの Redirect URI を更新済み ✅
  - `https://piscine-tracker.tsunanko.workers.dev/auth/callback`

---

## ステップ 1: wrangler CLI インストール

```bash
npm install -g wrangler
```

## ステップ 2: Cloudflare にログイン

```bash
cd /Users/ichippe/42tokyo/9.work/intra_time_mesure_claude/workers
wrangler login
```

→ ブラウザが開く → Cloudflare アカウントで認証（無料アカウントでOK）

## ステップ 3: KV Namespace を作成（セッション保存用）

```bash
wrangler kv namespace create SESSIONS
```

出力例：
```
🌀 Creating namespace with title "piscine-tracker-SESSIONS"
✨ Success!
Add the following to your wrangler.toml:
[[kv_namespaces]]
binding = "SESSIONS"
id = "abc123def456..."   ← このIDをコピー
```

→ `wrangler.toml` の `REPLACE_WITH_KV_NAMESPACE_ID` をこのIDに書き換え

## ステップ 4: Secrets を登録

新しい42 Intra アプリの Client ID / Client Secret を登録:

```bash
wrangler secret put FORTY_TWO_CLIENT_ID
# プロンプトが出たら新しいアプリの Client ID を貼り付け

wrangler secret put FORTY_TWO_CLIENT_SECRET
# プロンプトが出たら新しいアプリの Client Secret を貼り付け

wrangler secret put REDIRECT_URI
# プロンプトが出たら以下を貼り付け:
# https://piscine-tracker.tsunanko.workers.dev/auth/callback
```

## ステップ 5: デプロイ

```bash
wrangler deploy
```

出力例：
```
✨ Success! Deployed to https://piscine-tracker.tsunanko.workers.dev
```

## ステップ 6: 動作確認

ブラウザで以下にアクセス:
https://piscine-tracker.tsunanko.workers.dev

→ ログイン画面が表示されれば成功！

---

## アクセス先URL（完成後）

- **認証付きURL**: https://piscine-tracker.tsunanko.workers.dev
- **GitHub Pages（直接）**: https://tsunanko.github.io/piscine-tracker/dashboard.html（認証なし）

GitHub Pages のURLは変わらず存在するため、
完全に保護したい場合はリポジトリを Private にする必要があります。
（Private にすると GitHub Pages は有料プランが必要）

---

## トラブルシューティング

### `wrangler login` でブラウザが開かない
```bash
wrangler login --no-browser
# 表示されたURLをコピーしてブラウザで開く
```

### KV IDを忘れた場合
```bash
wrangler kv namespace list
```
