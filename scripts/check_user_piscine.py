#!/usr/bin/env python3
"""指定ユーザーのPiscine cursus情報を表示するワンショットスクリプト"""
import os, sys, requests

INTRA_API_BASE = "https://api.intra.42.fr"
TOKEN_URL = f"{INTRA_API_BASE}/oauth/token"

def get_token():
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": os.environ["CLIENT_ID"],
        "client_secret": os.environ["CLIENT_SECRET"],
    })
    resp.raise_for_status()
    return resp.json()["access_token"]

login = sys.argv[1] if len(sys.argv) > 1 else "hirosuzu"
token = get_token()
headers = {"Authorization": f"Bearer {token}"}

# ユーザーのcursus情報を取得
resp = requests.get(f"{INTRA_API_BASE}/v2/users/{login}/cursus_users", headers=headers)
resp.raise_for_status()
for cu in resp.json():
    cursus = cu.get("cursus", {})
    print(f"cursus_id={cursus.get('id')} name={cursus.get('name')}")
    print(f"  begin_at: {cu.get('begin_at')}")
    print(f"  end_at:   {cu.get('end_at')}")
    print(f"  level:    {cu.get('level')}")
    print()
