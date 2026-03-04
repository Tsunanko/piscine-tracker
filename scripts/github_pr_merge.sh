#!/bin/bash
# GitHub PRマージスクリプト
# 使い方: ./scripts/github_pr_merge.sh <PR番号>

REPO="Tsunanko/piscine-tracker"
PR_NUMBER="${1:-}"

if [ -z "$GITHUB_TOKEN" ]; then
  echo "ERROR: GITHUB_TOKEN が設定されていません"
  exit 1
fi

if [ -z "$PR_NUMBER" ]; then
  echo "使い方: $0 <PR番号>"
  exit 1
fi

RESPONSE=$(curl -s -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/$REPO/pulls/$PR_NUMBER/merge" \
  -d '{"merge_method":"merge"}')

MERGED=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('merged', False))")
MESSAGE=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message',''))")

if [ "$MERGED" = "True" ]; then
  echo "PR #$PR_NUMBER をマージしました"
else
  echo "マージ失敗: $MESSAGE"
  exit 1
fi
