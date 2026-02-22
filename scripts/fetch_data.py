#!/usr/bin/env python3
"""
GitHub Actions用データ取得スクリプト
42 APIからPiscine生のデータを取得してJSONファイルに保存する

最適化:
- Step3(locations_stats×147回) を Step4 に統合して重複を排除
- level を cursus_users レスポンスから抽出（追加API呼び出しゼロ）
- scale_teams でレビュー回数を取得（Step3削除分と相殺）
- 偏差値計算をポスト処理で一括実施
"""

import json
import os
import re
import statistics
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

# Load .env if it exists (for local testing)
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

# --- Configuration ---
INTRA_API_BASE = "https://api.intra.42.fr"
TOKEN_URL = f"{INTRA_API_BASE}/oauth/token"

JST = timezone(timedelta(hours=9))
PISCINE_START = datetime(2026, 2, 2, 0, 0, 0, tzinfo=JST)
PISCINE_END   = datetime(2026, 2, 28, 0, 0, 0, tzinfo=JST)
PISCINE_DAYS  = 26
TARGET_HOURS_PER_DAY = 8

CAMPUS_ID         = 26
PISCINE_CURSUS_ID = 9

OUTPUT_DIR = "public"

# 偏差値計算: 時間の母集団フィルタ（直近7日間に1h以上来た学生のみ）
ACTIVE_DAYS_THRESHOLD = 7
ACTIVE_HOURS_THRESHOLD = 1.0


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


def fetch_all_pages(token, path, params=None):
    """全ページを取得して結合する"""
    results = []
    page = 1
    base_params = dict(params or {})
    base_params["page[size]"] = 100
    while True:
        base_params["page[number]"] = page
        data = api_get(token, path, base_params)
        results.extend(data)
        print(f"  page {page}: {len(data)} items")
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.6)
    return results


def parse_host(host):
    if not host:
        return None, None
    name = host.split(".")[0]
    m = re.match(r"^(c\d+)(.*)", name)
    if m:
        return m.group(1), m.group(2)
    return None, None


def parse_duration(s):
    if not s:
        return 0.0
    parts = s.split(":")
    if len(parts) != 3:
        return 0.0
    return int(parts[0]) + int(parts[1]) / 60 + float(parts[2]) / 3600


def calc_deviation(x, mean, std):
    """偏差値を計算: 50 + 10 * (x - mean) / std"""
    if std == 0:
        return 50.0
    return round(50 + 10 * (x - mean) / std, 1)


def main():
    print("=== Piscine Data Fetcher ===")
    now = datetime.now(JST)
    print(f"Time: {now.isoformat()}")

    token = get_token()
    print("Token acquired")

    # 1. Piscine生一覧取得（levelも同時に取得）
    print("\n[1] Fetching piscine students (with level)...")
    cursus_users = fetch_all_pages(token, f"/v2/cursus/{PISCINE_CURSUS_ID}/cursus_users", {
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
        image = (user.get("image") or {})
        image_small = image.get("versions", {}).get("small", "") if image else ""
        students[login] = {
            "login": login,
            "display_name": user.get("usual_full_name") or user.get("displayname", ""),
            "image_small": image_small or "",
            "total_hours": 0,
            "level": round(item.get("level", 0), 2),  # cursus_usersから直接取得
            "daily": [],  # 偏差値計算で使用するため保存
        }
    print(f"  Total piscine students: {len(students)}")

    # 2. アクティブロケーション取得
    print("\n[2] Fetching active locations...")
    locations_raw = fetch_all_pages(token, f"/v2/campus/{CAMPUS_ID}/locations", {
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
    os.makedirs(f"{OUTPUT_DIR}/data", exist_ok=True)

    loc_params = {
        "begin_at": PISCINE_START.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "end_at":   PISCINE_END.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    login_list = list(students.keys())
    user_jsons = {}  # 偏差値計算後にまとめて書き込む

    for i, login in enumerate(login_list):
        try:
            # --- locations_stats (旧Step3+Step4統合) ---
            stats = api_get(token, f"/v2/users/{login}/locations_stats", loc_params)
            daily_hours = {}
            total_hours_from_stats = 0.0
            for date_str, dur in stats.items():
                h = parse_duration(dur)
                if h > 0:
                    daily_hours[date_str] = h
                    total_hours_from_stats += h

            students[login]["total_hours"] = round(total_hours_from_stats, 2)
            total_hours = students[login]["total_hours"]

            # アクティブセッション考慮
            is_active = login in active_map
            active_extra_hours = 0.0
            if is_active:
                begin_str = active_map[login].get("begin_at", "")
                try:
                    begin = datetime.fromisoformat(begin_str.replace("Z", "+00:00"))
                    active_extra_hours = max(0, (now.astimezone(timezone.utc) - begin).total_seconds() / 3600)
                    total_hours = round(total_hours + active_extra_hours, 2)
                except Exception:
                    pass

            # メトリクス計算
            total_target = TARGET_HOURS_PER_DAY * PISCINE_DAYS
            if now < PISCINE_START:
                elapsed_days = 0
                elapsed_hours = 0
            elif now > PISCINE_END:
                elapsed_hours = (PISCINE_END - PISCINE_START).total_seconds() / 3600
                elapsed_days = PISCINE_DAYS
            else:
                elapsed_hours = (now - PISCINE_START).total_seconds() / 3600
                elapsed_days = elapsed_hours / 24

            avg_hours_per_day = total_hours / elapsed_days if elapsed_days > 0 else 0
            remaining_total = max(0, total_target - total_hours)
            if now >= PISCINE_END:
                remaining_days = 0
                required_avg = 0
            else:
                remaining_secs = (PISCINE_END - max(now, PISCINE_START)).total_seconds()
                remaining_days = remaining_secs / 86400
                required_avg = remaining_total / remaining_days if remaining_days > 0 else 0

            progress_pct = min(100, (total_hours / total_target) * 100) if total_target > 0 else 0
            expected_hours = (elapsed_hours / 24) * TARGET_HOURS_PER_DAY if elapsed_hours > 0 else 0
            diff = total_hours - expected_hours

            # 日別データ（偏差値計算用にstudentsにも保存）
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

            time.sleep(0.3)  # locations_stats の後

            # --- プロジェクト取得 ---
            try:
                projects_raw = api_get(token, f"/v2/users/{login}/projects_users", {
                    "page[size]": 50,
                })
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

            time.sleep(0.3)  # projects_users の後

            # --- scale_teams でレビュー回数取得 ---
            review_given = 0
            try:
                scale_teams_raw = api_get(token, f"/v2/users/{login}/scale_teams", {
                    "page[size]": 100,
                    "range[begin_at]": f"{PISCINE_START.strftime('%Y-%m-%d')},{PISCINE_END.strftime('%Y-%m-%d')}",
                })
                review_given = sum(
                    1 for s in scale_teams_raw
                    if s.get("corrector", {}).get("login") == login
                    and s.get("filled_at")
                )
            except Exception as e:
                print(f"  [WARN] {login} scale_teams failed: {e}")

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
                "daily": daily,
                "projects": projects,
                "updated_at": now.isoformat(),
                # level_deviation, hours_deviation はポスト処理で追加
            }
            user_jsons[login] = user_json

        except Exception as e:
            print(f"  [ERROR] {login}: {type(e).__name__}: {e}")
            # fetch失敗をNoneでマーク（0と区別する）
            students[login]["total_hours"] = None
            students[login]["fetch_failed"] = True
            # リトライは1回だけ実施
            time.sleep(2)
            try:
                print(f"  [RETRY] {login}...")
                stats = api_get(token, f"/v2/users/{login}/locations_stats", loc_params)
                total_hours_from_stats = sum(parse_duration(dur) for dur in stats.values())
                students[login]["total_hours"] = round(total_hours_from_stats, 2)
                students[login]["fetch_failed"] = False
                print(f"  [RETRY OK] {login}: {students[login]['total_hours']:.1f}h")
            except Exception as e2:
                print(f"  [RETRY FAIL] {login}: {e2}")

        if (i + 1) % 20 == 0 or (i + 1) == len(login_list):
            print(f"  {i + 1}/{len(login_list)} done")
        time.sleep(0.3)  # scale_teams の後（ループ末尾）

    # 4. 偏差値計算（ポスト処理）
    print("\n[4] Calculating deviation scores...")

    # レベル偏差値: 全学生（level > 0）を母集団
    all_levels = [s["level"] for s in students.values() if s.get("level", 0) > 0]
    if len(all_levels) >= 2:
        level_mean = statistics.mean(all_levels)
        level_std = statistics.stdev(all_levels) if statistics.stdev(all_levels) > 0 else 1
    else:
        level_mean, level_std = 0.0, 1.0
    print(f"  Level: mean={level_mean:.2f}, std={level_std:.2f}, n={len(all_levels)}")

    # 時間偏差値: 直近7日間に{ACTIVE_HOURS_THRESHOLD}h以上来た学生のみを母集団
    seven_days_ago = (now - timedelta(days=ACTIVE_DAYS_THRESHOLD)).strftime("%Y-%m-%d")
    active_hours_list = []
    for login, s in students.items():
        daily = s.get("daily", [])
        if any(d["date"] >= seven_days_ago and d["hours"] >= ACTIVE_HOURS_THRESHOLD for d in daily):
            active_hours_list.append(s["total_hours"])

    if len(active_hours_list) >= 2:
        hours_mean = statistics.mean(active_hours_list)
        hours_std = statistics.stdev(active_hours_list) if statistics.stdev(active_hours_list) > 0 else 1
    else:
        hours_mean, hours_std = 0.0, 1.0
    print(f"  Hours (active {len(active_hours_list)} students): mean={hours_mean:.1f}h, std={hours_std:.1f}h")

    # レビュー偏差値: データが取れた全学生を母集団（0回を含む）
    all_reviews = [uj.get("review_given", 0) for uj in user_jsons.values()]
    if len(all_reviews) >= 2:
        review_mean = statistics.mean(all_reviews)
        review_std = statistics.stdev(all_reviews) if statistics.stdev(all_reviews) > 0 else 1
    else:
        review_mean, review_std = 0.0, 1.0
    print(f"  Review: mean={review_mean:.1f}, std={review_std:.1f}, n={len(all_reviews)}")

    # 各学生に偏差値を付与
    for login, uj in user_jsons.items():
        level = students[login].get("level", 0)
        hours = students[login].get("total_hours", 0)
        reviews = uj.get("review_given", 0)
        level_dev = calc_deviation(level, level_mean, level_std)
        hours_dev = calc_deviation(hours, hours_mean, hours_std)
        review_dev = calc_deviation(reviews, review_mean, review_std)
        composite_dev = round((level_dev + hours_dev + review_dev) / 3, 1)
        uj["level_deviation"] = level_dev
        uj["hours_deviation"] = hours_dev
        uj["review_deviation"] = review_dev
        uj["composite_deviation"] = composite_dev
        # dashboard JSON 生成で使うためstudentsにも保存
        students[login]["level_deviation"] = level_dev
        students[login]["hours_deviation"] = hours_dev
        students[login]["review_deviation"] = review_dev
        students[login]["composite_deviation"] = composite_dev
        students[login]["review_given"] = reviews

    # 5. 個人JSONファイルを一括書き込み
    print(f"\n[5] Writing {len(user_jsons)} per-user JSON files...")
    for login, uj in user_jsons.items():
        with open(f"{OUTPUT_DIR}/data/{login}.json", "w") as f:
            json.dump(uj, f, ensure_ascii=False)

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
        entry = {
            "login": s["login"],
            "display_name": s["display_name"],
            "image_small": s["image_small"],
            "total_hours": None if failed else s["total_hours"],  # 取得失敗はNoneで区別
            "level": s["level"],
            "level_deviation": None if failed else s.get("level_deviation", 50.0),
            "hours_deviation": None if failed else s.get("hours_deviation", 50.0),
            "review_deviation": None if failed else s.get("review_deviation", 50.0),
            "composite_deviation": None if failed else s.get("composite_deviation", 50.0),
            "review_given": None if failed else s.get("review_given", 0),
            "fetch_failed": failed,
        }
        if login in online_logins:
            loc = active_map[login]
            online.append({**entry, **loc})
        else:
            offline.append(entry)

    online.sort(key=lambda x: x.get("total_hours") or 0, reverse=True)
    offline.sort(key=lambda x: x.get("total_hours") or 0, reverse=True)

    dashboard = {
        "online": online,
        "offline": offline,
        "total_students": len(all_students),
        "total_online": len(online),
        "hours_loading": False,
        "cached_at": now.isoformat(),
    }

    with open(f"{OUTPUT_DIR}/data.json", "w") as f:
        json.dump(dashboard, f, ensure_ascii=False)

    print(f"  Wrote public/data.json ({len(online)} online, {len(offline)} offline)")
    print("\nDone!")


if __name__ == "__main__":
    main()
