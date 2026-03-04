#!/bin/bash
# GitHub Actions ワークフロー実行スクリプト
# 使い方: ./scripts/github_run_workflow.sh <workflow_file> [branch]

REPO="Tsunanko/piscine-tracker"
WORKFLOW="${1:-update-data.yml}"
BRANCH="${2:-main}"

if [ -z "$GITHUB_TOKEN" ]; then
  echo "ERROR: GITHUB_TOKEN が設定されていません"
  exit 1
fi

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/$REPO/actions/workflows/$WORKFLOW/dispatches" \
  -d "{\"ref\":\"$BRANCH\"}")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -1)

if [ "$HTTP_CODE" = "204" ]; then
  echo "ワークフロー '$WORKFLOW' を実行しました (branch: $BRANCH)"
  echo "確認: https://github.com/$REPO/actions"
else
  echo "実行失敗 (HTTP $HTTP_CODE): $BODY"
  exit 1
fi
