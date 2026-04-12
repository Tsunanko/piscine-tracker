#!/usr/bin/env python3
"""
GitHub Actions用データ取得スクリプト
42 APIからPiscine生のデータを取得してJSONファイルに保存する

処理の流れ:
  Step 1: Piscine生一覧取得 (/v2/cursus/9/cursus_users)
  Step 2: アクティブロケーション取得 (/v2/campus/26/locations)
  Step 3: 学生ごとの詳細データ取得 (locations_stats + projects + scale_teams)
  Step 4: 偏差値計算（全員分をまとめて計算）
  Step 5: 個人JSONファイルを一括書き込み
  Step 6: 全体ダッシュボード用 data.json を生成

API呼び出し数の最適化:
  - level を cursus_users レスポンスから直接取得（追加API呼び出しゼロ）
  - scale_teams でレビュー回数を取得（Piscine期間でフィルタ済み）
  - 偏差値計算をポスト処理で一括実施（各学生ごとに計算しない）

実行環境:
  - GitHub Actions (Ubuntu) から1日16回呼び出される
  - ローカルテスト時は .env ファイルの CLIENT_ID/SECRET を使用
"""

import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

# ─── ローカルテスト用: .env ファイルの読み込み ────────────────────────────
# GitHub Actions では環境変数 CLIENT_ID/CLIENT_SECRET が Secrets から注入される
# ローカルでテストする場合は .env ファイルに書いておく（.gitignore で除外済み）
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                # setdefault: すでに環境変数がある場合は上書きしない
                os.environ.setdefault(key.strip(), val.strip())

# ─── 設定値 ──────────────────────────────────────────────────────────────────
INTRA_API_BASE = "https://api.intra.42.fr"
TOKEN_URL = f"{INTRA_API_BASE}/oauth/token"

JST = timezone(timedelta(hours=9))  # 日本標準時 (UTC+9)

# PISCINE_MONTH: どの月のPiscineを処理するか（環境変数で切り替え）
# "02" → 2月Piscine（2026-02-02〜2026-02-27）
# "03" → 3月Piscine（2026-03-16〜2026-04-10）
PISCINE_MONTH = os.environ.get("PISCINE_MONTH", "02")

_PISCINE_CONFIG = {
    "2303": {
        "start": datetime(2023, 3, 6,  0, 0, 0, tzinfo=JST),
        "end":   datetime(2023, 4, 1,  0, 0, 0, tzinfo=JST),  # 3/31の翌日
        "days":  26,
    },
    "2408": {
        "start": datetime(2024, 8, 5,  0, 0, 0, tzinfo=JST),
        "end":   datetime(2024, 8, 31, 0, 0, 0, tzinfo=JST),  # 8/30の翌日
        "days":  26,
    },
    "2409": {
        "start": datetime(2024, 9, 2,  0, 0, 0, tzinfo=JST),  # 仮日付（要API確認）
        "end":   datetime(2024, 9, 28, 0, 0, 0, tzinfo=JST),  # 9/27の翌日（仮）
        "days":  26,
    },
    "2502": {
        "start": datetime(2025, 2, 3,  0, 0, 0, tzinfo=JST),
        "end":   datetime(2025, 3, 1,  0, 0, 0, tzinfo=JST),  # 2/28の翌日
        "days":  26,
    },
    "2503": {
        "start": datetime(2025, 3, 11, 0, 0, 0, tzinfo=JST),
        "end":   datetime(2025, 4, 6,  0, 0, 0, tzinfo=JST),  # 4/5の翌日
        "days":  26,
    },
    "02": {
        "start": datetime(2026, 2, 2,  0, 0, 0, tzinfo=JST),
        "end":   datetime(2026, 2, 28, 0, 0, 0, tzinfo=JST),  # 最終日の翌日
        "days":  26,
    },
    "03": {
        "start": datetime(2026, 3, 16, 0, 0, 0, tzinfo=JST),
        "end":   datetime(2026, 4, 11, 0, 0, 0, tzinfo=JST),  # 4/10の翌日
        "days":  26,
    },
}

if PISCINE_MONTH not in _PISCINE_CONFIG:
    raise ValueError(f"Unsupported PISCINE_MONTH: {PISCINE_MONTH}. Use '2408', '2409', '02', or '03'.")

PISCINE_START = _PISCINE_CONFIG[PISCINE_MONTH]["start"]
PISCINE_END   = _PISCINE_CONFIG[PISCINE_MONTH]["end"]
PISCINE_DAYS  = _PISCINE_CONFIG[PISCINE_MONTH]["days"]
TARGET_HOURS_PER_DAY = 8    # 1日の目標学習時間

CAMPUS_ID         = 26  # 42 Tokyo のキャンパスID
PISCINE_CURSUS_ID = 9   # Piscine のカリキュラムID

WORKER_URL    = os.environ.get("WORKER_URL", "https://piscine-tracker.tsunanko.workers.dev")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")

# 偏差値計算: アクティブ学生の判定条件
# Piscine終了後は PISCINE_END を基準に使う（終了後7日以上経過で母集団が空になるバグ防止）
ACTIVE_DAYS_THRESHOLD  = 3    # 直近何日間を見るか
ACTIVE_HOURS_THRESHOLD = 1.0  # 何時間以上来たら「アクティブ」とみなすか


def upload_to_kv(payload, max_retries=3):
    """Worker API 経由でデータを Cloudflare KV にアップロードする。

    payload 例:
      { "type": "summary", "data": {...} }               → data.json 相当
      { "type": "user", "login": "xxx", "data": {...} }  → data/{login}.json 相当

    WORKER_SECRET が未設定の場合はスキップ（ローカルデバッグ用）。
    5xx エラーの場合は最大 max_retries 回リトライする（指数バックオフ）。
    """
    if not WORKER_SECRET:
        print("  [SKIP] WORKER_SECRET not set, skipping KV upload")
        return
    last_err = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{WORKER_URL}/api/kv/upload",
                json=payload,
                headers={"Authorization": f"Bearer {WORKER_SECRET}"},
                timeout=30,
            )
            if not resp.ok:
                # エラー詳細をログに出力（Workerが返したエラーメッセージ）
                try:
                    err_body = resp.json()
                    err_detail = err_body.get("detail", err_body.get("error", ""))
                except Exception:
                    err_detail = resp.text[:300]
                login_hint = payload.get("login", payload.get("type", "?"))
                if resp.status_code >= 500 and attempt < max_retries - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    print(f"  [WARN] KV {resp.status_code} for {login_hint}: {err_detail} → retry in {wait}s")
                    time.sleep(wait)
                    last_err = requests.HTTPError(f"{resp.status_code}: {err_detail}", response=resp)
                    continue
                else:
                    print(f"  [ERROR] KV {resp.status_code} for {login_hint}: {err_detail}")
                    resp.raise_for_status()
            return  # 成功
        except requests.exceptions.Timeout:
            wait = 2 ** attempt
            print(f"  [WARN] KV upload timeout (attempt {attempt+1}) → retry in {wait}s")
            time.sleep(wait)
            last_err = Exception("Timeout")
        except requests.HTTPError:
            raise  # 既にログ済みなので再raiseのみ
        except Exception as e:
            raise
    raise last_err or Exception("KV upload failed after retries")


_current_token = None  # モジュールレベルでトークンを保持（401時にリフレッシュ可能にする）


def get_token():
    """42 API の Client Credentials フローでアクセストークンを取得する。

    Client Credentials フロー: ユーザーの認証なしでサーバー間通信に使うOAuth方式。
    GitHub Actions（サーバー側）がデータ取得する際はこちらを使う。
    ユーザーのデータ取得（ブラウザ側）は Authorization Code Flow を使う。
    """
    global _current_token
    client_id     = os.environ["CLIENT_ID"]
    client_secret = os.environ["CLIENT_SECRET"]
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "client_credentials",  # サーバー間認証
        "client_id":     client_id,
        "client_secret": client_secret,
    })
    resp.raise_for_status()  # エラー時は HTTPError を raise
    _current_token = resp.json()["access_token"]
    return _current_token


def refresh_token():
    """トークンを再取得する（401エラー時に呼ばれる）。"""
    print("  [AUTH] Token expired, refreshing...")
    return get_token()


def api_get(token, path, params=None, _retry=3):
    """42 API に GET リクエストを送る。

    Authorization: Bearer {token} ヘッダーを付けてリクエストする。
    42 API はページネーション（page[size], page[number]）を使う。
    429 Too Many Requests の場合は指数バックオフでリトライする。
    401 Unauthorized の場合はトークンをリフレッシュしてリトライする。
    """
    global _current_token
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(_retry):
        resp = requests.get(f"{INTRA_API_BASE}{path}", headers=headers, params=params)
        if resp.status_code == 429:
            wait = 15 * (2 ** attempt)  # 15s, 30s, 60s
            print(f"  [429] rate limited on {path} → wait {wait}s (attempt {attempt+1}/{_retry})")
            time.sleep(wait)
            continue
        if resp.status_code == 401 and attempt < _retry:
            # トークン期限切れ → リフレッシュしてリトライ
            token = refresh_token()
            _current_token = token
            headers = {"Authorization": f"Bearer {token}"}
            time.sleep(1)
            continue
        resp.raise_for_status()
        return resp.json()
    # 全リトライ失敗
    resp.raise_for_status()
    return resp.json()


def fetch_all_pages(token, path, params=None):
    """42 API のページネーションを処理して全データを取得する。

    42 API は1回のリクエストで最大100件しか返さない。
    100件返ってきたら「次のページがある」と判断して繰り返す。
    最後のページが100件未満なら終了。

    API制限対策として各ページ取得後に0.6秒待つ。
    注: api_get 内でトークンがリフレッシュされた場合、
        _current_token が更新されるので以降のページ取得にも反映される。
    """
    results = []
    page = 1
    base_params = dict(params or {})
    base_params["page[size]"] = 100  # 1ページあたりの最大件数
    while True:
        base_params["page[number]"] = page
        data = api_get(_current_token, path, base_params)
        results.extend(data)
        print(f"  page {page}: {len(data)} items")
        if len(data) < 100:
            break  # 100件未満 = 最終ページ
        page += 1
        time.sleep(0.6)  # API レート制限を避けるための待機
    return results


def parse_host(host):
    """座席ホスト名をクラスター番号と座席番号に分解する。

    例: "c1r5s5.42tokyo.jp" → cluster="c1", seat="r5s5"
        "c2r3s10" → cluster="c2", seat="r3s10"
        None → (None, None)

    ホスト名のフォーマット: c{クラスター番号}r{行}s{列}
    """
    if not host:
        return None, None
    name = host.split(".")[0]  # "c1r5s5.42tokyo.jp" → "c1r5s5"
    m = re.match(r"^(c\d+)(.*)", name)  # "c1" と "r5s5" に分割
    if m:
        return m.group(1), m.group(2)
    return None, None


def parse_duration(s):
    """42 API の時間文字列を時間（float）に変換する。

    42 API の locations_stats は "HH:MM:SS" 形式で時間を返す。
    例: "9:30:00" → 9.5 (時間)
        "1:15:30" → 1.258... (時間)
        "" or None → 0.0
    """
    if not s:
        return 0.0
    parts = s.split(":")
    if len(parts) != 3:
        return 0.0
    # 時間 + 分/60 + 秒/3600 = 小数点付き時間
    return int(parts[0]) + int(parts[1]) / 60 + float(parts[2]) / 3600


def calc_deviation(x, mean, std):
    """偏差値を計算する。

    偏差値の公式: 50 + 10 × (値 - 平均) / 標準偏差

    偏差値の意味:
      50 = ちょうど平均
      60 = 平均より 1 標準偏差高い（上位約16%）
      70 = 平均より 2 標準偏差高い（上位約2%）
      40 = 平均より 1 標準偏差低い（下位約16%）

    std == 0 の場合（全員同じ値）は 50.0 を返す。
    """
    if std == 0:
        return 50.0
    return round(50 + 10 * (x - mean) / std, 1)


def main():
    print("=== Piscine Data Fetcher ===")
    now = datetime.now(JST)
    print(f"Time: {now.isoformat()}")

    get_token()  # _current_token にトークンを保存
    print("Token acquired")


    # 1. Piscine生一覧取得（levelも同時に取得）
    print("\n[1] Fetching piscine students (with level)...")
    cursus_users = fetch_all_pages(_current_token, f"/v2/cursus/{PISCINE_CURSUS_ID}/cursus_users", {
        "filter[campus_id]": CAMPUS_ID,
        "range[begin_at]": f"{PISCINE_START.strftime('%Y-%m-%d')},{PISCINE_END.strftime('%Y-%m-%d')}",
        "sort": "user_id",
    })
    students = {}
    for item in cursus_users:
        user = item.get("user", {})
        login = user.get("login", "")
        if not login:
            continue
        new_level = round(item.get("level", 0), 2)
        # 重複エントリ対策: 同一loginが複数返ってきた場合はレベルが高い方を採用
        # （ページネーション境界の重複 or 複数Piscine登録が原因）
        if login in students:
            existing_level = students[login]["level"]
            if existing_level >= new_level:
                print(f"  [WARN] Duplicate cursus entry for {login}: keeping lv{existing_level} over lv{new_level}")
                continue
            print(f"  [WARN] Duplicate cursus entry for {login}: upgrading lv{existing_level} → lv{new_level}")
        image = (user.get("image") or {})
        image_small = image.get("versions", {}).get("small", "") if image else ""
        students[login] = {
            "login": login,
            "display_name": user.get("usual_full_name") or user.get("displayname", ""),
            "image_small": image_small or "",
            "total_hours": 0,
            "level": new_level,
            "daily": [],  # 偏差値計算で使用するため保存
        }
    print(f"  Total piscine students: {len(students)}")

    # 1b. 42cursus（本科）に在籍している学生を取得 → Piscine合格者の判定に使う
    # cursus_id=21 = 42 本カリキュラム（Piscine合格後に入学）
    # Piscine生の中でこのカリキュラムに存在する場合 → piscine_result = "passed"
    # 存在しない場合 → piscine_result = "failed"（結果未発表の場合は None）
    print("\n[1b] Fetching 42cursus students (piscine graduates)...")
    CURSUS_42_ID = 21  # 42 本カリキュラムのID
    graduated_logins = set()
    cursus42_by_login = {}  # login → cursus42 entry（不足学生の復元に使用）
    graduated_fetch_failed = False
    try:
        cursus42_users = fetch_all_pages(_current_token, f"/v2/cursus/{CURSUS_42_ID}/cursus_users", {
            "filter[campus_id]": CAMPUS_ID,
            "sort": "user_id",
        })
        for item in cursus42_users:
            user = item.get("user", {})
            login = user.get("login", "")
            if login:
                graduated_logins.add(login)
                cursus42_by_login[login] = item
        print(f"  42cursus students at campus {CAMPUS_ID}: {len(graduated_logins)}")
    except Exception as e:
        graduated_fetch_failed = True
        print(f"  [ERROR] Failed to fetch 42cursus students: {e}")
        print(f"  [WARN] piscine_result will be set to None for all students (cannot determine pass/fail)")

    # ── 合格したがpiscine cursusから消えた学生のカウント ──────────────────
    # 42 APIでは、ピシン合格後に本科(cursus_42)へ移行するとpiscine cursusエントリが
    # 削除される場合がある。passed_count が過少計上になるため別途カウントする。
    # ※ studentsには追加しない → 0h/Lv0で合格表示という誤解を招くカードを防ぐ
    # ※ begin_at フィルタでこのpiscine期間中に本科入学した学生のみを対象にする
    join_cutoff = PISCINE_START.strftime("%Y-%m-%d")
    missing_graduate_logins = set()
    for login in (graduated_logins - set(students.keys())):
        item = cursus42_by_login.get(login, {})
        begin_at = (item.get("begin_at") or "")[:10]  # YYYY-MM-DD
        if begin_at >= join_cutoff:
            missing_graduate_logins.add(login)
    if missing_graduate_logins:
        print(f"  [INFO] {len(missing_graduate_logins)} cursus_42 entries with begin_at>={join_cutoff} NOT in piscine cursus (treated as different cohort, excluded): {sorted(missing_graduate_logins)}")
    else:
        print(f"  [INFO] No such entries (join_cutoff={join_cutoff})")

    # Piscine生（147人）の中で42cursusに移行した人数を確認
    piscine_graduates = graduated_logins & set(students.keys())
    # results_announced: Piscine終了後かつ合格者が存在する場合のみ true
    # 進行中に誰かが移行しても「結果発表」扱いにしない
    # graduated_fetch_failed時は合否判定不能のためFalse
    results_announced = (not graduated_fetch_failed) and (now >= PISCINE_END) and (len(piscine_graduates) > 0)
    print(f"  Piscine graduates (in 42cursus): {len(piscine_graduates)}")
    print(f"  Results announced: {results_announced}")

    # 2. アクティブロケーション取得
    print("\n[2] Fetching active locations...")
    locations_raw = fetch_all_pages(_current_token, f"/v2/campus/{CAMPUS_ID}/locations", {
        "filter[active]": "true",
    })
    active_map = {}  # login -> location info
    for loc in locations_raw:
        user = loc.get("user", {})
        login = user.get("login", "")
        if not login:
            continue
        host = loc.get("host", "")
        cluster, seat = parse_host(host)
        active_map[login] = {
            "host": host,
            "cluster": cluster,
            "seat": seat,
            "begin_at": loc.get("begin_at", ""),
        }
    print(f"  Active locations: {len(active_map)}")

    # 3. 各Piscine生のデータ取得（locations_stats + projects + scale_teams を統合）
    #    ※ 旧Step3(locations_stats×147回)を削除し、旧Step4に統合
    #    ※ scale_teams追加分はStep3削除と相殺→合計API呼び出し数は同じ
    print("\n[3] Fetching per-student data (locations_stats + projects + scale_teams)...")
    loc_params = {
        "begin_at": PISCINE_START.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "end_at":   PISCINE_END.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    login_list = list(students.keys())
    user_jsons = {}  # 偏差値計算後にまとめて書き込む

    for i, login in enumerate(login_list):
        try:
            # --- locations_stats (旧Step3+Step4統合) ---
            stats = api_get(_current_token, f"/v2/users/{login}/locations_stats", loc_params)
            daily_hours = {}
            total_hours_from_stats = 0.0
            for date_str, dur in stats.items():
                h = parse_duration(dur)
                if h > 0:
                    daily_hours[date_str] = h
                    total_hours_from_stats += h

            students[login]["total_hours"] = round(total_hours_from_stats, 2)
            total_hours = students[login]["total_hours"]

            # ─── アクティブセッション（現在ログイン中）の処理 ───────────────
            # is_active: 現在クラスターにいる（Step2 で取得したロケーション情報に存在する）
            is_active = login in active_map
            active_extra_hours = 0.0
            if is_active:
                begin_str = active_map[login].get("begin_at", "")
                try:
                    begin = datetime.fromisoformat(begin_str.replace("Z", "+00:00"))
                    # 現在セッションの経過時間を計算（フロントエンドの「現在のセッション」表示用）
                    # ※ locations_stats は進行中のセッションを含むため total_hours には加算しない
                    active_extra_hours = max(0, (now.astimezone(timezone.utc) - begin).total_seconds() / 3600)
                except Exception:
                    pass

            # ─── 進捗メトリクス計算 ───────────────────────────────────────
            total_target = TARGET_HOURS_PER_DAY * PISCINE_DAYS  # 目標総時間 (8h × 26日 = 208h)

            # 経過時間・経過日数（Piscine期間外の場合はクランプ）
            if now < PISCINE_START:
                # Piscine 開始前: 0日0時間経過
                elapsed_days = 0
                elapsed_hours = 0
            elif now > PISCINE_END:
                # Piscine 終了後: 最大値（PISCINE_DAYS）で固定
                elapsed_hours = (PISCINE_END - PISCINE_START).total_seconds() / 3600
                elapsed_days = PISCINE_DAYS
            else:
                # 進行中: 開始からの経過時間
                elapsed_hours = (now - PISCINE_START).total_seconds() / 3600
                elapsed_days = elapsed_hours / 24

            # 1日あたり平均学習時間 (経過日数が0の場合は0)
            avg_hours_per_day = total_hours / elapsed_days if elapsed_days > 0 else 0

            # 残り必要時間 (マイナスにならないよう max(0, ...) でクランプ)
            remaining_total = max(0, total_target - total_hours)

            # 残り日数・1日あたり必要時間
            if now >= PISCINE_END:
                remaining_days = 0
                required_avg = 0
            else:
                remaining_secs = (PISCINE_END - max(now, PISCINE_START)).total_seconds()
                remaining_days = remaining_secs / 86400  # 秒 → 日
                required_avg = remaining_total / remaining_days if remaining_days > 0 else 0

            # 進捗率 (0〜100%)
            progress_pct = min(100, (total_hours / total_target) * 100) if total_target > 0 else 0

            # 目標との乖離: 実績 - 期待値 (プラスなら目標超過、マイナスなら遅れ)
            # expected_hours = この時点までに来ているべき時間
            expected_hours = (elapsed_hours / 24) * TARGET_HOURS_PER_DAY if elapsed_hours > 0 else 0
            diff = total_hours - expected_hours  # プラス = on track、マイナス = behind

            # 日別データ（偏差値計算用にstudentsにも保存）
            today_str = now.strftime("%Y-%m-%d")  # JST の今日
            daily = []
            d = PISCINE_START
            while d < PISCINE_END:
                key = d.strftime("%Y-%m-%d")
                h = round(daily_hours.get(key, 0), 2)
                daily.append({
                    "date": key,
                    "weekday": d.strftime("%a"),
                    "hours": h,
                    "met_target": h >= TARGET_HOURS_PER_DAY,
                })
                d += timedelta(days=1)
            students[login]["daily"] = daily  # 偏差値計算で使用

            time.sleep(0.5)  # locations_stats の後

            # --- プロジェクト取得（全ページ取得・期間フィルタ付き）---
            # page[size]=50 だと本科移行後に100件超えるユーザーのPiscine最終試験が
            # 取得できないバグを修正 → fetch_all_pages + created_at range で全件取得
            proj_range_start = (PISCINE_START - timedelta(days=7)).strftime("%Y-%m-%d")
            proj_range_end   = (PISCINE_END   + timedelta(days=14)).strftime("%Y-%m-%d")
            try:
                try:
                    projects_raw = fetch_all_pages(_current_token, f"/v2/users/{login}/projects_users", {
                        "range[created_at]": f"{proj_range_start},{proj_range_end}",
                    })
                except Exception as proj_e:
                    # 429 Too Many Requests → 10秒待ってリトライ
                    if getattr(getattr(proj_e, 'response', None), 'status_code', 0) == 429 or "429" in str(proj_e):
                        print(f"  [WARN] {login} projects 429, retry in 10s...")
                        time.sleep(10)
                        projects_raw = fetch_all_pages(_current_token, f"/v2/users/{login}/projects_users", {
                            "range[created_at]": f"{proj_range_start},{proj_range_end}",
                        })
                    else:
                        raise
                projects = []
                for p in projects_raw:
                    proj_name = p.get("project", {}).get("name", "")
                    proj_slug = p.get("project", {}).get("slug", "")
                    status = p.get("status", "")
                    validated = p.get("validated?", False)
                    final_mark = p.get("final_mark")
                    if proj_name:
                        projects.append({
                            "name": proj_name,
                            "slug": proj_slug,
                            "status": status,
                            "validated": validated,
                            "final_mark": final_mark,
                        })
                def proj_sort_key(p):
                    if p["status"] == "in_progress":
                        return (0, p["name"])
                    elif p["status"] == "waiting_for_correction":
                        return (1, p["name"])
                    elif p["validated"]:
                        return (2, p["name"])
                    else:
                        return (3, p["name"])
                projects.sort(key=proj_sort_key)
            except Exception as e:
                print(f"  [WARN] {login} projects failed: {e}")
                projects = []

            # Piscine Final Exam スコア抽出
            exam_project = next((p for p in projects if "final exam" in p["name"].lower()), None)
            students[login]["exam_score"] = exam_project["final_mark"] if exam_project else None

            # rush/bsq/Cシリーズ プロジェクト完了数の集計
            # rush: "Rush 00", "Rush 01", "Rush 02" 等のペア課題
            # bsq: "BSQ" チーム課題
            # c_projects: "C 00" 〜 "C 13" 個人課題
            rush_completed = sum(1 for p in projects if "rush" in p["name"].lower() and p.get("validated"))
            rush_attempted = sum(1 for p in projects if "rush" in p["name"].lower() and p.get("status") in ("finished", "waiting_for_correction", "in_progress"))
            # BSQ: 完全一致ではなく部分一致で検索（"C Piscine BSQ" のような名前に対応）
            bsq_completed = 1 if any("bsq" in p["name"].lower() and p.get("validated") for p in projects) else 0
            bsq_attempted = 1 if any("bsq" in p["name"].lower() and p.get("status") in ("finished", "waiting_for_correction", "in_progress") for p in projects) else 0
            # Cシリーズ: "C Piscine C 00" のように末尾が "c <数字>" で終わるプロジェクトを対象
            # re.search で末尾パターンを検索（"^c\s*\d+$" はプレフィックス付き名称に対応できないため修正）
            c_completed = sum(1 for p in projects if re.search(r"\bc\s+\d+$", p["name"].lower()) and p.get("validated"))
            students[login]["rush_completed"] = rush_completed
            students[login]["rush_attempted"] = rush_attempted
            students[login]["bsq_completed"] = bsq_completed
            students[login]["bsq_attempted"] = bsq_attempted
            students[login]["c_completed"] = c_completed

            # Rush 個別追跡: Rush 00/01/02 それぞれの登録・完了状況
            # "Rush 00", "Rush 01", "Rush 02" のような名前にマッチ
            RUSH_STATUS = ("finished", "waiting_for_correction", "in_progress")
            for rush_num in ["00", "01", "02"]:
                pattern = re.compile(rf"\brush\s+{rush_num}\b", re.IGNORECASE)
                rush_p = next((p for p in projects if pattern.search(p["name"])), None)
                students[login][f"rush_{rush_num}_attempted"] = 1 if (rush_p and rush_p.get("status") in RUSH_STATUS) else 0
                students[login][f"rush_{rush_num}_completed"] = 1 if (rush_p and rush_p.get("validated")) else 0

            time.sleep(0.3)  # projects_users の後

            # --- scale_teams でレビュー回数・フラグ・評価スコア取得 ---
            review_given         = 0  # 評価者として実施したレビュー数
            outstanding_received = 0  # 自分のプロジェクトが "Outstanding project" と評価された回数
            cheat_received       = 0  # 自分のプロジェクトに "Cheat" フラグが付いた回数
            # 被評価者から評価者への4項目スコア（Interested/Nice/Punctuality/Rigorous）
            rating_scores      = []   # 総合満足度（1-5）
            nice_scores        = []   # 感じの良さ（0-4）
            rigorous_scores    = []   # 厳密さ（0-4）
            interested_scores  = []   # 興味・関心（0-4）
            punctuality_scores = []   # 時間厳守（0-4）
            try:
                scale_teams_raw = api_get(_current_token, f"/v2/users/{login}/scale_teams", {
                    "page[size]": 100,
                    "range[begin_at]": f"{PISCINE_START.strftime('%Y-%m-%d')},{PISCINE_END.strftime('%Y-%m-%d')}",
                })
                for team in scale_teams_raw:
                    if not team.get("filled_at"):
                        continue
                    corrector_login  = team.get("corrector", {}).get("login", "")
                    corrected_logins = [c.get("login", "") for c in team.get("correcteds", [])]
                    flag_name        = (team.get("flag") or {}).get("name", "")

                    if corrector_login == login:
                        # このユーザーが評価者 → レビュー回数 & 評価スコアを収集
                        review_given += 1
                        for fb in (team.get("feedbacks") or []):
                            r = fb.get("rating")
                            if r is not None:
                                rating_scores.append(r)
                            for detail in (fb.get("feedback_details") or []):
                                kind = detail.get("kind", "")
                                rate = detail.get("rate")
                                if rate is not None:
                                    if kind == "nice":
                                        nice_scores.append(rate)
                                    elif kind == "rigorous":
                                        rigorous_scores.append(rate)
                                    elif kind == "interested":
                                        interested_scores.append(rate)
                                    elif kind == "punctuality":
                                        punctuality_scores.append(rate)

                    if login in corrected_logins:
                        # このユーザーが被評価者 → 自分の作品へのフラグを集計
                        if flag_name == "Outstanding project":
                            outstanding_received += 1
                        elif flag_name == "Cheat":
                            cheat_received += 1

            except Exception as e:
                print(f"  [WARN] {login} scale_teams failed: {e}")

            # 評価スコア平均（レビューを受けていない場合は None）
            def _avg(lst):
                return round(sum(lst) / len(lst), 2) if lst else None
            avg_rating      = _avg(rating_scores)       # 総合満足度（1-5）
            avg_nice        = _avg(nice_scores)          # 感じの良さ（0-4）
            avg_rigorous    = _avg(rigorous_scores)      # 厳密さ（0-4）
            avg_interested  = _avg(interested_scores)    # 興味・関心（0-4）
            avg_punctuality = _avg(punctuality_scores)   # 時間厳守（0-4）

            time.sleep(0.3)  # scale_teams の後

            # --- イベント参加数取得 ---
            events_attended = 0
            try:
                events_raw = api_get(_current_token, f"/v2/users/{login}/events_users", {
                    "page[size]": 100,
                    "range[created_at]": f"{PISCINE_START.strftime('%Y-%m-%d')},{PISCINE_END.strftime('%Y-%m-%d')}",
                })
                events_attended = len(events_raw)
            except Exception as e:
                if getattr(getattr(e, 'response', None), 'status_code', 0) == 429 or "429" in str(e):
                    print(f"  [WARN] {login} events 429, retry in 10s...")
                    time.sleep(10)
                    try:
                        events_raw = api_get(_current_token, f"/v2/users/{login}/events_users", {
                            "page[size]": 100,
                            "range[created_at]": f"{PISCINE_START.strftime('%Y-%m-%d')},{PISCINE_END.strftime('%Y-%m-%d')}",
                        })
                        events_attended = len(events_raw)
                    except Exception as e2:
                        print(f"  [WARN] {login} events retry failed: {e2}")
                else:
                    print(f"  [WARN] {login} events failed: {e}")
            students[login]["events_attended"] = events_attended

            # Piscine合否判定
            # results_announced=True の場合のみ合否を確定する（未発表の場合は None）
            if results_announced:
                piscine_result = "passed" if login in graduated_logins else "failed"
            else:
                piscine_result = None
            students[login]["piscine_result"] = piscine_result

            # 在籍状況: 42本科(cursus_42)でのステータスを判定
            # 判定基準: end_at フィールド（cursusが終了したかどうか）
            # - end_at = null   → 在籍中 ("active")  ← blackholed_at が過去でも在籍中の場合あり
            # - end_at = 設定済 + blackholed_at あり → BH ("blackholed")
            # - end_at = 設定済 + blackholed_at なし → 自主退学 ("withdrawn")
            # - cursus_42 に存在しない → None（Piscine不合格 or 別ルート）
            # ※ blackholed_at は残り日数の計算にのみ使用（active判定には使わない）
            cursus42_entry = cursus42_by_login.get(login)
            if cursus42_entry is not None:
                end_at_str = cursus42_entry.get("end_at")
                blackholed_at_str = cursus42_entry.get("blackholed_at")
                current_42_level = round(cursus42_entry.get("level", 0), 2)
                grade_42 = cursus42_entry.get("grade")  # "Learner" | "Member" | null
                if end_at_str:
                    enrollment_42 = "blackholed" if blackholed_at_str else "withdrawn"
                    blackhole_days_left = None
                else:
                    enrollment_42 = "active"
                    if blackholed_at_str:
                        bh_dt = datetime.fromisoformat(blackholed_at_str.replace("Z", "+00:00"))
                        days_left = (bh_dt - now.astimezone(timezone.utc)).days
                        blackhole_days_left = days_left if days_left > 0 else None
                    else:
                        blackhole_days_left = None
                # コモンコア完了判定:
                # grade="Member" が公式判定、level>=21 をフォールバックで使用
                common_core_done = (grade_42 == "Member") or (current_42_level >= 21.0)
            else:
                enrollment_42 = None
                current_42_level = None
                grade_42 = None
                blackhole_days_left = None
                common_core_done = False
            still_at_42 = enrollment_42 == "active"
            students[login]["still_at_42"] = still_at_42
            students[login]["enrollment_42"] = enrollment_42
            students[login]["current_42_level"] = current_42_level
            students[login]["grade_42"] = grade_42
            students[login]["common_core_done"] = common_core_done
            students[login]["blackhole_days_left"] = blackhole_days_left

            # user_json 構築（偏差値はポスト処理で追加）
            user_json = {
                "login": login,
                "display_name": students[login]["display_name"],
                "image_small": students[login]["image_small"],
                "level": students[login]["level"],
                "piscine_start": PISCINE_START.strftime("%Y-%m-%d"),
                "piscine_end": (PISCINE_END - timedelta(days=1)).strftime("%Y-%m-%d"),
                "piscine_days": PISCINE_DAYS,
                "target_hours_per_day": TARGET_HOURS_PER_DAY,
                "total_target_hours": total_target,
                "total_logged_hours": round(total_hours, 2),
                "elapsed_days": round(elapsed_days, 2),
                "elapsed_hours": round(elapsed_hours, 2),
                "avg_hours_per_day": round(avg_hours_per_day, 2),
                "remaining_days": round(remaining_days, 2),
                "remaining_hours_needed": round(remaining_total, 2),
                "required_avg_remaining": round(required_avg, 2),
                "progress_pct": round(progress_pct, 1),
                "diff_from_target": round(diff, 2),
                "on_track": diff >= 0,
                "is_active": is_active,
                "active_extra_hours": round(active_extra_hours, 2),
                "review_given": review_given,
                "outstanding_received": outstanding_received,  # Outstanding flagをもらった回数
                "cheat_received": cheat_received,              # Cheatフラグをもらった回数
                "avg_rating":      avg_rating,      # 総合満足度（1-5）
                "avg_nice":        avg_nice,         # 感じの良さ（0-4）
                "avg_rigorous":    avg_rigorous,     # 厳密さ（0-4）
                "avg_interested":  avg_interested,   # 興味・関心（0-4）
                "avg_punctuality": avg_punctuality,  # 時間厳守（0-4）
                "events_attended": events_attended,   # Piscine期間中のイベント参加数
                "daily": daily,
                "projects": projects,
                "piscine_result": piscine_result,       # "passed" | "failed" | null
                "results_announced": results_announced, # 合否発表済みフラグ
                "enrollment_42": enrollment_42,             # "active"|"blackholed"|"withdrawn"|null
                "still_at_42": still_at_42,                 # 後方互換: active=Trueのみ True
                "current_42_level": current_42_level,       # 現在の42本科レベル
                "grade_42": grade_42,                       # "Learner"|"Member"|null
                "common_core_done": common_core_done,       # コモンコア完了フラグ
                "blackhole_days_left": blackhole_days_left, # BHまでの残り日数（active時のみ）
                "updated_at": now.isoformat(),
                # level_deviation, hours_deviation はポスト処理で追加
            }
            user_jsons[login] = user_json

        except Exception as e:
            print(f"  [ERROR] {login}: {type(e).__name__}: {e}")
            # fetch失敗をNoneでマーク（0と区別する）
            students[login]["total_hours"] = None
            students[login]["fetch_failed"] = True
            # ★ piscine_result / still_at_42 はAPIエラーと無関係（graduated_logins判定は常に有効）
            #   ここで設定しないと合格者がpassed_countに入らなくなるバグを防ぐ
            if results_announced:
                students[login]["piscine_result"] = "passed" if login in graduated_logins else "failed"
            else:
                students[login]["piscine_result"] = None
            cursus42_entry_err = cursus42_by_login.get(login)
            if cursus42_entry_err is not None:
                end_at_err = cursus42_entry_err.get("end_at")
                bh_str_err = cursus42_entry_err.get("blackholed_at")
                if end_at_err:
                    enroll_err = "blackholed" if bh_str_err else "withdrawn"
                    bh_days_err = None
                else:
                    enroll_err = "active"
                    if bh_str_err:
                        bh_dt_err = datetime.fromisoformat(bh_str_err.replace("Z", "+00:00"))
                        _d = (bh_dt_err - now.astimezone(timezone.utc)).days
                        bh_days_err = _d if _d > 0 else None  # 過去日付は非表示
                    else:
                        bh_days_err = None
                lv_err = round(cursus42_entry_err.get("level", 0), 2)
                gr_err = cursus42_entry_err.get("grade")
                students[login]["enrollment_42"] = enroll_err
                students[login]["still_at_42"] = enroll_err == "active"
                students[login]["current_42_level"] = lv_err
                students[login]["grade_42"] = gr_err
                students[login]["common_core_done"] = (gr_err == "Member") or (lv_err >= 21.0)
                students[login]["blackhole_days_left"] = bh_days_err
            else:
                students[login]["enrollment_42"] = None
                students[login]["still_at_42"] = False
                students[login]["current_42_level"] = None
                students[login]["grade_42"] = None
                students[login]["common_core_done"] = False
                students[login]["blackhole_days_left"] = None
            # リトライは1回だけ実施
            time.sleep(2)
            try:
                print(f"  [RETRY] {login}...")
                stats = api_get(_current_token, f"/v2/users/{login}/locations_stats", loc_params)
                total_hours_from_stats = sum(parse_duration(dur) for dur in stats.values())
                students[login]["total_hours"] = round(total_hours_from_stats, 2)
                students[login]["fetch_failed"] = False
                print(f"  [RETRY OK] {login}: {students[login]['total_hours']:.1f}h")
            except Exception as e2:
                print(f"  [RETRY FAIL] {login}: {e2}")

        if (i + 1) % 20 == 0 or (i + 1) == len(login_list):
            print(f"  {i + 1}/{len(login_list)} done")
        time.sleep(0.3)  # events_users の後（ループ末尾）

    # 4. 偏差値計算（ポスト処理）
    print("\n[4] Calculating deviation scores...")

    # ── アクティブ学生の判定 ──────────────────────────────────────────────
    # Piscine進行中: PISCINE_END基準の直近ACTIVE_DAYS_THRESHOLD日間に1h以上来た学生
    # アクティブ判定: Piscine最終日（PISCINE_END-1日）を基準に直近7日間に1h以上来た学生
    # 設計意図: 序盤で離脱した学生を除き、最終週まで継続して参加した学生を母集団とする。
    # ピシン進行中・終了後いずれも同じ基準で固定（実行タイミングに依存しない安定した母集団）。
    # 例: 最終日=2/27 → 対象期間 2/21〜2/27 の7日間に1h以上来た学生
    # Piscine進行中は「今日」を基準にする（未来の日付だと誰もアクティブにならないバグ対策）
    # Piscine終了後は最終日固定（安定した母集団）
    reference_time = min(now, PISCINE_END - timedelta(days=1))
    window_start = (reference_time - timedelta(days=ACTIVE_DAYS_THRESHOLD - 1)).strftime("%Y-%m-%d")
    active_logins = set()
    for login, s in students.items():
        daily = s.get("daily", [])
        if any(d["date"] >= window_start and d["hours"] >= ACTIVE_HOURS_THRESHOLD for d in daily):
            active_logins.add(login)
    print(f"  Active (last {ACTIVE_DAYS_THRESHOLD}d before piscine end: {window_start}〜{reference_time.strftime('%Y-%m-%d')}, {ACTIVE_HOURS_THRESHOLD}h+): {len(active_logins)} students")

    # レベル偏差値: アクティブ学生（level > 0）を母集団
    active_levels = [s["level"] for login, s in students.items()
                     if login in active_logins and s.get("level", 0) > 0]
    if len(active_levels) >= 2:
        level_mean = statistics.mean(active_levels)
        level_std  = statistics.stdev(active_levels) or 1.0
        level_ok   = True
    else:
        level_mean, level_std, level_ok = 0.0, 1.0, False
    print(f"  Level: mean={level_mean:.2f}, std={level_std:.2f}, n={len(active_levels)}")

    # 時間偏差値: アクティブ学生（total_hours > 0）を母集団
    active_hours = [s["total_hours"] for login, s in students.items()
                    if login in active_logins
                    and s.get("total_hours") is not None and s["total_hours"] > 0]
    if len(active_hours) >= 2:
        hours_mean = statistics.mean(active_hours)
        hours_std  = statistics.stdev(active_hours) or 1.0
        hours_ok   = True
    else:
        hours_mean, hours_std, hours_ok = 0.0, 1.0, False
    print(f"  Hours: mean={hours_mean:.1f}h, std={hours_std:.1f}h, n={len(active_hours)}")

    # レビュー偏差値: アクティブ学生を母集団（0回含む）
    active_reviews = [user_jsons[login].get("review_given", 0)
                      for login in active_logins if login in user_jsons]
    if len(active_reviews) >= 2:
        review_mean = statistics.mean(active_reviews)
        review_std  = statistics.stdev(active_reviews) or 1.0
        review_ok   = True
    else:
        review_mean, review_std, review_ok = 0.0, 1.0, False
    print(f"  Review: mean={review_mean:.1f}, std={review_std:.1f}, n={len(active_reviews)}")

    # 各学生に偏差値を付与（母集団 < 2 の場合は null → フロントで '-' 表示）
    for login, uj in user_jsons.items():
        level   = students[login].get("level", 0)
        hours   = students[login].get("total_hours") or 0
        reviews = uj.get("review_given", 0)
        level_dev   = calc_deviation(level,   level_mean,  level_std)  if level_ok  else None
        hours_dev   = calc_deviation(hours,   hours_mean,  hours_std)  if hours_ok  else None
        review_dev  = calc_deviation(reviews, review_mean, review_std) if review_ok else None
        valid_devs  = [d for d in [level_dev, hours_dev, review_dev] if d is not None]
        composite_dev = round(sum(valid_devs) / len(valid_devs), 1) if valid_devs else None
        uj["level_deviation"]     = level_dev
        uj["hours_deviation"]     = hours_dev
        uj["review_deviation"]    = review_dev
        uj["composite_deviation"] = composite_dev
        # dashboard JSON 生成で使うためstudentsにも保存
        students[login]["level_deviation"]    = level_dev
        students[login]["hours_deviation"]    = hours_dev
        students[login]["review_deviation"]   = review_dev
        students[login]["composite_deviation"] = composite_dev
        students[login]["review_given"]           = reviews
        students[login]["outstanding_received"]   = uj.get("outstanding_received", 0)
        students[login]["cheat_received"]         = uj.get("cheat_received", 0)
        students[login]["avg_rating"]             = uj.get("avg_rating")
        students[login]["avg_nice"]               = uj.get("avg_nice")
        students[login]["avg_rigorous"]           = uj.get("avg_rigorous")
        students[login]["avg_interested"]         = uj.get("avg_interested")
        students[login]["avg_punctuality"]        = uj.get("avg_punctuality")

    # 5. 個人JSONを Cloudflare KV にアップロード（バッチ方式: 1回のリクエストで全員分）
    # 旧方式（147回個別送信）は Cloudflare KV 無料プランの 1,000回/日 上限をすぐ消費するため廃止。
    # 新方式: 全ユーザーJSONを1つのオブジェクト {login: data} にまとめて1回で送信 → KV書き込みは1回のみ。
    print(f"\n[5] Uploading {len(user_jsons)} per-user JSONs to KV (batch mode)...")
    try:
        upload_to_kv({"type": "users_batch", "month": PISCINE_MONTH, "data": user_jsons})
        print(f"  Uploaded all {len(user_jsons)} user JSONs as batch (1 KV write)")
    except Exception as e:
        print(f"  [ERROR] Batch KV upload failed: {e}")
        print(f"  [INFO] Falling back to individual uploads...")
        ok_count = 0
        for login, uj in user_jsons.items():
            try:
                upload_to_kv({"type": "user", "login": login, "data": uj})
                ok_count += 1
            except Exception as e2:
                print(f"  [ERROR] KV upload failed for {login}: {e2}")
        print(f"  Uploaded {ok_count}/{len(user_jsons)} user JSONs (individual fallback)")
        if ok_count == 0:
            print("  [FATAL] All KV uploads failed. Aborting to prevent data loss.")
            sys.exit(1)

    # 6. ダッシュボード用 data.json 生成
    print("\n[6] Writing dashboard data.json...")
    all_students = list(students.values())
    online_logins = set(active_map.keys()) & set(students.keys())

    online = []
    offline = []
    for s in all_students:
        login = s["login"]
        # dashboardに必要なフィールドのみ（dailyは除く）
        failed = s.get("fetch_failed", False)
        # 1時間以上来た日数（1日平均ログイン時間の計算に使用）
        daily = s.get("daily", [])
        active_days = len([d for d in daily if d.get("hours", 0) >= 1.0])
        entry = {
            "login": s["login"],
            "display_name": s["display_name"],
            "image_small": s["image_small"],
            "total_hours": None if failed else s["total_hours"],  # 取得失敗はNoneで区別
            "level": s["level"],
            "level_deviation": None if failed else s.get("level_deviation"),
            "hours_deviation": None if failed else s.get("hours_deviation"),
            "review_deviation": None if failed else s.get("review_deviation"),
            "composite_deviation": None if failed else s.get("composite_deviation"),
            "review_given": None if failed else s.get("review_given", 0),
            "outstanding_received": None if failed else s.get("outstanding_received", 0),
            "cheat_received":       None if failed else s.get("cheat_received", 0),
            "avg_rating":           None if failed else s.get("avg_rating"),
            "avg_nice":             None if failed else s.get("avg_nice"),
            "avg_rigorous":         None if failed else s.get("avg_rigorous"),
            "avg_interested":       None if failed else s.get("avg_interested"),
            "avg_punctuality":      None if failed else s.get("avg_punctuality"),
            "exam_score": None if failed else s.get("exam_score"),
            "rush_completed": None if failed else s.get("rush_completed", 0),
            "rush_attempted": None if failed else s.get("rush_attempted", 0),
            "rush_00_attempted": None if failed else s.get("rush_00_attempted", 0),
            "rush_00_completed": None if failed else s.get("rush_00_completed", 0),
            "rush_01_attempted": None if failed else s.get("rush_01_attempted", 0),
            "rush_01_completed": None if failed else s.get("rush_01_completed", 0),
            "rush_02_attempted": None if failed else s.get("rush_02_attempted", 0),
            "rush_02_completed": None if failed else s.get("rush_02_completed", 0),
            "bsq_completed":  None if failed else s.get("bsq_completed", 0),
            "bsq_attempted":  None if failed else s.get("bsq_attempted", 0),
            "events_attended": None if failed else s.get("events_attended", 0),
            "c_completed":    None if failed else s.get("c_completed", 0),
            "is_active": login in active_logins,  # 直近7日1h以上来ているか（偏差値母集団フラグ）
            "active_days": None if failed else active_days,  # 1h以上来た日数（1日平均計算用）
            "fetch_failed": failed,
            "piscine_result": s.get("piscine_result"),  # "passed" | "failed" | null
            "enrollment_42": s.get("enrollment_42"),               # "active"|"blackholed"|"withdrawn"|null
            "still_at_42": s.get("still_at_42", False),        # 後方互換
            "current_42_level": s.get("current_42_level"),     # 現在の42本科レベル
            "grade_42": s.get("grade_42"),                      # "Learner"|"Member"|null
            "common_core_done": s.get("common_core_done", False), # コモンコア完了フラグ
            "blackhole_days_left": s.get("blackhole_days_left"), # BHまでの残り日数
        }
        if login in online_logins:
            loc = active_map[login]
            online.append({**entry, **loc})
        else:
            offline.append(entry)

    online.sort(key=lambda x: x.get("total_hours") or 0, reverse=True)
    offline.sort(key=lambda x: x.get("total_hours") or 0, reverse=True)

    # 合否集計（piscine cursus 在籍者のうち、fetch_failedでないもののみ対象）
    passed_count = sum(1 for s in all_students if s.get("piscine_result") == "passed" and not s.get("fetch_failed"))
    failed_count = sum(1 for s in all_students if s.get("piscine_result") == "failed" and not s.get("fetch_failed"))

    dashboard = {
        "online": online,
        "offline": offline,
        "total_students": len(all_students),  # piscine cursus在籍の147人
        "total_online": len(online),
        "active_count": len(active_logins),          # 偏差値計算の母集団人数
        "deviation_base": {                           # 偏差値計算条件（stats.html表示用）
            "active_days": ACTIVE_DAYS_THRESHOLD,
            "active_hours_threshold": ACTIVE_HOURS_THRESHOLD,
        },
        "passed_count": passed_count,                # Piscine合格者数
        "failed_count": failed_count,                # Piscine不合格者数
        "results_announced": results_announced,      # 合否発表済みかどうか
        "hours_loading": False,
        "cached_at": now.isoformat(),
    }

    try:
        upload_to_kv({"type": "summary", "month": PISCINE_MONTH, "data": dashboard})
        print(f"  Uploaded data.json to KV ({len(online)} online, {len(offline)} offline)")
    except Exception as e:
        print(f"  [ERROR] KV upload failed for data.json: {e}")

    # ─── ローカル開発用: WORKER_SECRET 未設定時は dev-data.json に書き出す ───
    # これにより fetch_data.py をローカル実行するだけで stats.html がテスト可能
    if not WORKER_SECRET:
        dev_data_path = Path(__file__).parent.parent / "public" / "dev-data.json"
        with open(dev_data_path, "w", encoding="utf-8") as f:
            json.dump(dashboard, f, ensure_ascii=False, indent=2)
        print(f"  [DEV] Written to {dev_data_path} ({len(online)} online, {len(offline)} offline)")

    print("\nDone!")


if __name__ == "__main__":
    main()
