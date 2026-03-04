# Claude Code 設定

## GitHub操作のセットアップ

### 必要なもの: GitHub Personal Access Token (PAT)

**トークン作成手順:**
1. https://github.com/settings/tokens → "Generate new token (classic)"
2. 以下のスコープにチェック:
   - `repo` (フルアクセス)
   - `workflow` (GitHub Actions の実行)
3. 生成されたトークンをコピー

**トークンの設定:**
```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxx
```

セッションをまたいで使うには `~/.bashrc` か `~/.zshrc` に追記:
```bash
echo 'export GITHUB_TOKEN=ghp_xxxxxxxxxxxxx' >> ~/.bashrc
```

---

## リポジトリ情報

- **GitHub:** https://github.com/Tsunanko/piscine-tracker
- **GitHub Pages:** https://tsunanko.github.io/piscine-tracker
- **Cloudflare Worker:** https://piscine-tracker.tsunanko.workers.dev

## Claude が使えるGitHub操作

`GITHUB_TOKEN` が設定されていれば以下が可能:

- PRの作成・マージ
- GitHub Actions ワークフローのトリガー（Update Piscine Data など）
- Issues・PRのコメント

### 使用コマンド例
```bash
# PR作成
scripts/github_pr_create.sh "タイトル" "ブランチ名" "本文"

# PRマージ
scripts/github_pr_merge.sh <PR番号>

# Update Piscine Data 実行
scripts/github_run_workflow.sh update-data.yml
```
