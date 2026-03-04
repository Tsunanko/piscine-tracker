#!/bin/bash
# GitHub PR作成スクリプト
# 使い方: ./scripts/github_pr_create.sh "タイトル" "head_branch" "base_branch" "本文"

REPO="Tsunanko/piscine-tracker"
TITLE="${1:-}"
HEAD="${2:-}"
BASE="${3:-main}"
BODY="${4:-}"

if [ -z "$GITHUB_TOKEN" ]; then
  echo "ERROR: GITHUB_TOKEN が設定されていません"
  echo "export GITHUB_TOKEN=ghp_xxxxx を実行してください"
  exit 1
fi

if [ -z "$TITLE" ] || [ -z "$HEAD" ]; then
  echo "使い方: $0 \"タイトル\" \"head_branch\" [base_branch] [\"本文\"]"
  exit 1
fi

RESPONSE=$(curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/$REPO/pulls" \
  -d "$(python3 -c "import json,sys; print(json.dumps({'title': sys.argv[1], 'head': sys.argv[2], 'base': sys.argv[3], 'body': sys.argv[4]}))" "$TITLE" "$HEAD" "$BASE" "$BODY")")

PR_NUMBER=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('number','ERROR: '+str(d.get('message',d))))")
PR_URL=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('html_url',''))" 2>/dev/null)

echo "PR #$PR_NUMBER 作成完了: $PR_URL"
echo "$PR_NUMBER"
