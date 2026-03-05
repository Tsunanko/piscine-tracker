#!/usr/bin/env python3
"""
合否取り込みスクリプト（軽量版）

42 APIで「Piscine受講生が本科cursus(21)に登録されたか」をチェックし、
合否結果を data/results.json に蓄積する。

合否判定ロジック:
  合格 = 本科cursus(42cursus, cursus_id=21) に登録されている
  不合格 = Piscineカリキュラム上でblackholeまたはinactive（今後追加予定）
  未定 = まだいずれも確認できない（最多）

実行タイミング:
  GitHub Actions で Piscine終了後〜1ヶ月間、1日1回実行。
  新たに合格者が確認できたらcommit&pushで記録が蓄積される。

出力:
  data/results.json - 合否の蓄積記録（gitで履歴管理）
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ─── ローカルテスト用: .env 読み込み ─────────────────────────────────────────
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

# ─── 設定 ─────────────────────────────────────────────────────────────────────
INTRA_API_BASE    = "https://api.intra.42.fr"
TOKEN_URL         = f"{INTRA_API_BASE}/oauth/token"
JST               = timezone(timedelta(hours=9))
CAMPUS_ID         = 26   # 42 Tokyo
PISCINE_CURSUS_ID = 9    # Piscineカリキュラム
MAIN_CURSUS_ID    = 21   # 42本科カリキュラム

# Piscine期間（学生一覧の絞り込みに使用）
PISCINE_START = datetime(2026, 2, 2, 0, 0, 0, tzinfo=JST)
PISCINE_END   = datetime(2026, 2, 28, 0, 0, 0, tzinfo=JST)

# 合否結果ファイル（gitで履歴管理）
RESULTS_FILE = Path(__file__).parent.parent / "data" / "results.json"

# Piscine生一覧ファイル（fetch_data.py が生成する data.json から取得）
PUBLIC_DATA_JSON = Path(__file__).parent.parent / "public" / "data.json"


def get_token():
    client_id     = os.environ["CLIENT_ID"]
    client_secret = os.environ["CLIENT_SECRET"]
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get(token, path, params=None):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{INTRA_API_BASE}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_all_pages(token, path, params=None, page_size=100):
    params = dict(params or {})
    params["page[size]"] = page_size
    results = []
    page = 1
    while True:
        params["page[number]"] = page
        data = api_get(token, path, params)
        if not data:
            break
        results.extend(data)
        if len(data) < page_size:
            break
        page += 1
        time.sleep(0.3)
    return results


def load_results() -> dict:
    """既存の results.json を読み込む。なければ空を返す。"""
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE) as f:
            return json.load(f)
    return {"updated_at": None, "students": {}}


def save_results(results: dict):
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def main():
    now = datetime.now(JST)
    print(f"[fetch_results] Start: {now.isoformat()}")

    token = get_token()
    print("Token acquired")

    # ── Step 1: Piscine生ログイン一覧を取得 ──────────────────────────────────
    # public/data.json が存在すればそこから（API呼び出し節約）
    piscine_logins: list[str] = []

    if PUBLIC_DATA_JSON.exists():
        with open(PUBLIC_DATA_JSON) as f:
            pub = json.load(f)
        # data.json の online/offline フィールドから全ログインを収集
        for s in pub.get("online", []) + pub.get("offline", []):
            login = s.get("login")
            if login:
                piscine_logins.append(login)
        print(f"Loaded {len(piscine_logins)} logins from public/data.json")
    else:
        # data.json がなければ API から取得
        print("public/data.json not found, fetching from API...")
        cursus_users = fetch_all_pages(token, f"/v2/cursus/{PISCINE_CURSUS_ID}/cursus_users", {
            "filter[campus_id]": CAMPUS_ID,
            "range[begin_at]": f"{PISCINE_START.strftime('%Y-%m-%d')},{PISCINE_END.strftime('%Y-%m-%d')}",
        })
        piscine_logins = [
            item["user"]["login"]
            for item in cursus_users
            if item.get("user", {}).get("login")
        ]
        print(f"Fetched {len(piscine_logins)} logins from API")

    # ── Step 2: 既存の results.json を読み込む ───────────────────────────────
    results = load_results()
    students = results.get("students", {})

    # 既存データから「まだ未確定（passed=null）」の学生のみ再チェック対象とする
    # → 合格確定済み（passed=true）はスキップしてAPI呼び出しを節約
    pending_logins = [
        login for login in piscine_logins
        if students.get(login, {}).get("passed") is not True
    ]
    print(f"Pending (not yet confirmed passed): {len(pending_logins)}/{len(piscine_logins)}")

    # ── Step 3: 本科cursus登録確認 ───────────────────────────────────────────
    # 本科cursus(21)の登録者をキャンパスでフィルタして一括取得
    # → 個別API呼び出しより大幅にAPI節約
    print(f"\nFetching main cursus (id={MAIN_CURSUS_ID}) students for campus {CAMPUS_ID}...")
    # Piscine終了日から3ヶ月以内に本科登録された人を対象
    after = PISCINE_END.strftime("%Y-%m-%d")
    before = (PISCINE_END + timedelta(days=90)).strftime("%Y-%m-%d")

    main_cursus_users = fetch_all_pages(token, f"/v2/cursus/{MAIN_CURSUS_ID}/cursus_users", {
        "filter[campus_id]": CAMPUS_ID,
        "range[begin_at]": f"{after},{before}",
    })
    passed_logins = {
        item["user"]["login"]
        for item in main_cursus_users
        if item.get("user", {}).get("login")
    }
    print(f"Found {len(passed_logins)} students enrolled in main cursus")

    # ── Step 4: results.json を更新 ─────────────────────────────────────────
    newly_passed = []
    changed = False

    for login in piscine_logins:
        existing = students.get(login, {})

        if login in passed_logins:
            if existing.get("passed") is not True:
                students[login] = {
                    "passed": True,
                    "confirmed_at": now.isoformat(),
                }
                newly_passed.append(login)
                changed = True
                print(f"  [NEW PASS] {login}")
        else:
            # まだ本科登録なし → passed=false(未定)として記録
            if login not in students:
                students[login] = {
                    "passed": False,
                    "confirmed_at": None,
                }
                changed = True

    print(f"\nNewly confirmed passed: {len(newly_passed)}")
    print(f"Total passed so far: {sum(1 for s in students.values() if s.get('passed'))}/{len(students)}")

    results["students"] = students
    results["updated_at"] = now.isoformat()
    results["summary"] = {
        "total": len(students),
        "passed": sum(1 for s in students.values() if s.get("passed") is True),
        "pending": sum(1 for s in students.values() if s.get("passed") is not True),
    }

    save_results(results)
    print(f"\nSaved to {RESULTS_FILE}")

    # GitHub Actions 側でdiffを見てcommitするかを判断できるように
    # 変更があったかどうかをexit codeで伝える（0=変更あり, 2=変更なし）
    if not changed and not newly_passed:
        print("No changes detected.")
        exit(2)

    print("Done.")


if __name__ == "__main__":
    main()
