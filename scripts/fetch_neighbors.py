#!/usr/bin/env python3
"""
座席隣接分析スクリプト（管理者専用）

Piscine期間中に「同じクラスター・同じ行・同じ時間帯」にいた学生ペアを分析し、
ピアラーニング多様性と合格率の相関を算出する。

API呼び出し最適化:
  - /v2/campus/{id}/locations を一括取得（個別ユーザー呼び出し不要）
  - 147人×個別 → キャンパス一括で約50ページに削減

処理の流れ:
  Step 1: 42 API トークン取得
  Step 2: キャンパス全体のロケーション履歴を一括取得
  Step 3: セッション解析（同行・同時間帯ペアの検出）
  Step 4: 統計算出（個人指標 + 全体統計 + 合格率相関）
  Step 5: JSON出力 + KVアップロード（オプション）

使い方:
  # ローカル実行（.envにCLIENT_ID/CLIENT_SECRETを設定）
  python scripts/fetch_neighbors.py

  # GitHub Actions（環境変数から自動取得）
  python scripts/fetch_neighbors.py
"""

import json
import os
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

# ─── .env 読み込み ────────────────────────────────────────────────────────────
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

# ─── 設定値 ──────────────────────────────────────────────────────────────────
INTRA_API_BASE = "https://api.intra.42.fr"
TOKEN_URL = f"{INTRA_API_BASE}/oauth/token"
JST = timezone(timedelta(hours=9))

CAMPUS_ID = 26  # 42 Tokyo

# Piscine 期間（環境変数で切り替え可能）
# PISCINE_MONTH=03 で3月Piscine、2408 で2024年8月Piscine、デフォルトは02
PISCINE_MONTH = os.environ.get("PISCINE_MONTH", "02")
_PISCINE_CONFIG = {
    "2408": {"start": datetime(2024, 8, 5,  0, 0, 0, tzinfo=JST),
             "end":   datetime(2024, 8, 31, 0, 0, 0, tzinfo=JST),
             "label": "2024-08 Piscine"},
    "02":   {"start": datetime(2026, 2, 2,  0, 0, 0, tzinfo=JST),
             "end":   datetime(2026, 2, 28, 0, 0, 0, tzinfo=JST),
             "label": "2026-02 Piscine"},
    "03":   {"start": datetime(2026, 3, 16, 0, 0, 0, tzinfo=JST),
             "end":   datetime(2026, 4, 11, 0, 0, 0, tzinfo=JST),
             "label": "2026-03 Piscine"},
}
if PISCINE_MONTH not in _PISCINE_CONFIG:
    raise ValueError(f"Unknown PISCINE_MONTH: {PISCINE_MONTH}. Use '2408', '02', or '03'.")
PISCINE_START = _PISCINE_CONFIG[PISCINE_MONTH]["start"]
PISCINE_END   = _PISCINE_CONFIG[PISCINE_MONTH]["end"]
PISCINE_LABEL = _PISCINE_CONFIG[PISCINE_MONTH]["label"]

WORKER_URL    = os.environ.get("WORKER_URL", "https://piscine-tracker.tsunanko.workers.dev")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")

# 隣接判定: 時間重複の最小閾値（分）
MIN_OVERLAP_MINUTES = 30

# 近接度の定義（優先度順）:
#   adjacent     (rank 0): 同行・seat差 == 1 (直隣り)               weight 1.0
#   near         (rank 1): 同行・seat差 == 2 (2個隣)                weight 0.7
#   facing       (rank 2): |row差| == 1・seat差 ≤ 1 (向かい側)      weight 0.5
#   same_row     (rank 3): 同行・seat差 ≥ 3 (同列遠め)              weight 0.2
#   same_cluster (rank 4): |row差| ≥ 2 (同クラスター別行)            weight 0.1
PROX_RANK   = {"adjacent": 0, "near": 1, "facing": 2, "same_row": 3, "same_cluster": 4}
PROX_WEIGHT = {"adjacent": 1.0, "near": 0.7, "facing": 0.5, "same_row": 0.2, "same_cluster": 0.1}

# ─── 共通関数（fetch_data.py と同じパターン）────────────────────────────────

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


def api_get(token, path, params=None, _retry=3):
    """42 API GET。429時は指数バックオフでリトライ。"""
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(_retry):
        resp = requests.get(f"{INTRA_API_BASE}{path}", headers=headers, params=params)
        if resp.status_code == 429:
            wait = 15 * (2 ** attempt)  # 15s, 30s, 60s
            print(f"  [429] rate limited on {path} → wait {wait}s (attempt {attempt+1}/{_retry})")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


def fetch_all_pages(token, path, params=None):
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


def upload_to_kv(payload, max_retries=3):
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
                try:
                    err_detail = resp.json().get("detail", resp.json().get("error", ""))
                except Exception:
                    err_detail = resp.text[:300]
                if resp.status_code >= 500 and attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"  [WARN] KV {resp.status_code}: {err_detail} → retry in {wait}s")
                    time.sleep(wait)
                    last_err = Exception(f"{resp.status_code}: {err_detail}")
                    continue
                else:
                    print(f"  [ERROR] KV {resp.status_code}: {err_detail}")
                    resp.raise_for_status()
            return
        except requests.exceptions.Timeout:
            wait = 2 ** attempt
            print(f"  [WARN] KV upload timeout (attempt {attempt+1}) → retry in {wait}s")
            time.sleep(wait)
            last_err = Exception("Timeout")
        except requests.HTTPError:
            raise
    raise last_err or Exception("KV upload failed after retries")


def parse_host_detailed(host):
    """座席ホスト名を (cluster, row, seat) の数値タプルに分解する。

    例: "c1r5s5.42tokyo.jp" → (1, 5, 5)
        "c2r3s10" → (2, 3, 10)
        None → (None, None, None)
    """
    if not host:
        return None, None, None
    name = host.split(".")[0]
    m = re.match(r"^c(\d+)r(\d+)s(\d+)$", name)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None, None, None


def compute_overlap_hours(begin_a, end_a, begin_b, end_b):
    """2つのセッションの重複時間（時間単位）を返す。"""
    overlap_start = max(begin_a, begin_b)
    overlap_end = min(end_a, end_b)
    overlap_seconds = max(0, (overlap_end - overlap_start).total_seconds())
    return overlap_seconds / 3600


def main():
    now = datetime.now(JST)
    print(f"=== Seat Neighbor Analysis ({PISCINE_LABEL}) ===")
    print(f"Time: {now.isoformat()}")
    print(f"Period: {PISCINE_START.strftime('%Y-%m-%d')} ~ {(PISCINE_END - timedelta(days=1)).strftime('%Y-%m-%d')}")

    # ─── Step 1: トークン取得 ────────────────────────────────────────────────
    print("\n[1] Getting API token...")
    token = get_token()
    print("  OK")

    # ─── Step 2: キャンパス全体のロケーション履歴を一括取得 ────────────────────
    # /v2/campus/{id}/locations を filter[active] なしで取得 → 全履歴が返る
    # range[begin_at] でPiscine期間に絞り込み
    print("\n[2] Fetching ALL campus location history (bulk)...")
    all_locations = fetch_all_pages(token, f"/v2/campus/{CAMPUS_ID}/locations", {
        "range[begin_at]": f"{PISCINE_START.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')},"
                           f"{PISCINE_END.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')}",
        "sort": "begin_at",
    })
    print(f"  Total location records: {len(all_locations)}")

    # ─── Step 2b: セッションデータのパース ────────────────────────────────────
    sessions = []  # list of { login, cluster, row, seat, begin, end }
    avatar_map = {}  # login → image_small URL（ロケーションデータから収集）
    skipped = 0
    for loc in all_locations:
        user = loc.get("user") or {}
        login = user.get("login", "")
        host = loc.get("host", "")
        begin_at = loc.get("begin_at")
        end_at = loc.get("end_at")

        if not login or not host or not begin_at:
            skipped += 1
            continue

        # アバターURL収集（ロケーションAPIのuserオブジェクトから）
        if login not in avatar_map:
            image = user.get("image") or {}
            img_small = image.get("versions", {}).get("small") if isinstance(image, dict) else None
            if img_small:
                avatar_map[login] = img_small

        cluster, row, seat = parse_host_detailed(host)
        if cluster is None:
            skipped += 1
            continue

        begin = datetime.fromisoformat(begin_at.replace("Z", "+00:00")).astimezone(JST)
        if end_at:
            end = datetime.fromisoformat(end_at.replace("Z", "+00:00")).astimezone(JST)
        else:
            end = now  # アクティブセッション → 現在時刻まで

        sessions.append({
            "login": login,
            "cluster": cluster,
            "row": row,
            "seat": seat,
            "begin": begin,
            "end": end,
        })

    print(f"  Parsed sessions: {len(sessions)} (skipped: {skipped})")

    # 全ユーザーのログインリスト
    all_logins = sorted(set(s["login"] for s in sessions))
    print(f"  Unique students: {len(all_logins)}")

    # ─── Step 3: 近接ペアの検出 ─────────────────────────────────────────────
    # 近接度の定義（PROX_RANK / PROX_WEIGHT は上部定数参照）:
    #   adjacent    : 同行・seat差 == 1 (直隣り)
    #   near        : 同行・seat差 == 2 (2個隣)
    #   facing      : |row差| == 1・seat差 ≤ 1 (向かい側)
    #   same_row    : 同行・seat差 ≥ 3 (同列遠め)
    #   same_cluster: |row差| ≥ 2 (同クラスター別行)
    print("\n[3] Detecting co-presence pairs (adjacent / near / facing / same_row / same_cluster)...")

    # インデックス: cluster → list of sessions
    cluster_index = defaultdict(list)
    for s in sessions:
        cluster_index[s["cluster"]].append(s)

    # neighbors[loginA][loginB] = { hours, weighted_hours, proximity }
    # proximity: 最も近い接触の種類（adjacent > near > facing > same_row > same_cluster）
    def _default_entry():
        return {"hours": 0.0, "weighted_hours": 0.0, "proximity": "same_cluster"}
    neighbors = defaultdict(lambda: defaultdict(_default_entry))
    pair_count = 0

    for cluster, group in cluster_index.items():
        n = len(group)
        for i in range(n):
            for j in range(i + 1, n):
                sa = group[i]
                sb = group[j]
                if sa["login"] == sb["login"]:
                    continue

                overlap = compute_overlap_hours(sa["begin"], sa["end"], sb["begin"], sb["end"])
                if overlap < MIN_OVERLAP_MINUTES / 60:
                    continue

                # 近接度判定
                row_diff = abs(sa["row"] - sb["row"])
                seat_diff = abs(sa["seat"] - sb["seat"])
                if row_diff == 0:
                    # 同じ行: seat差で分類
                    if seat_diff == 1:
                        prox = "adjacent"    # 直隣り
                    elif seat_diff == 2:
                        prox = "near"        # 2個隣
                    else:
                        prox = "same_row"    # 同行遠め
                elif row_diff == 1:
                    # 隣の行（向かい側）: seat差が1以内のみ facing
                    prox = "facing" if seat_diff <= 1 else "same_cluster"
                else:
                    prox = "same_cluster"

                # 既存エントリより近ければ proximity を更新
                entry_ab = neighbors[sa["login"]][sb["login"]]
                entry_ba = neighbors[sb["login"]][sa["login"]]
                if PROX_RANK[prox] < PROX_RANK[entry_ab["proximity"]]:
                    entry_ab["proximity"] = prox
                    entry_ba["proximity"] = prox
                # 実時間 + 重み付き時間を加算
                weight = PROX_WEIGHT[prox]
                entry_ab["hours"] += overlap
                entry_ba["hours"] += overlap
                entry_ab["weighted_hours"] += overlap * weight
                entry_ba["weighted_hours"] += overlap * weight
                pair_count += 1

    print(f"  Overlapping pairs found: {pair_count}")
    print(f"  Students with neighbors: {len(neighbors)}")

    # ─── Step 4: 統計算出 ────────────────────────────────────────────────────
    print("\n[4] Computing statistics...")

    # 各学生のログイン総時間（隣人密度計算用）
    total_hours_by_login = defaultdict(float)
    for s in sessions:
        hours = (s["end"] - s["begin"]).total_seconds() / 3600
        total_hours_by_login[s["login"]] += hours

    # 個人指標
    # avatar_map は Step 2b のセッションパース時に収集済み
    print(f"  Avatar URLs collected: {len(avatar_map)}")

    per_student = {}
    for login in all_logins:
        nbrs = neighbors.get(login, {})
        unique_neighbors = len(nbrs)
        total_neighbor_hours = sum(e["hours"] for e in nbrs.values())
        total_hours = total_hours_by_login.get(login, 0)

        # Top 15 隣人（近接度 → 重み付き時間の順でソート）
        def sort_key(item):
            l, e = item
            return (PROX_RANK[e["proximity"]], -e["weighted_hours"])
        top_neighbors_raw = sorted(nbrs.items(), key=sort_key)[:15]

        total_weighted_hours = sum(e["weighted_hours"] for e in nbrs.values())

        per_student[login] = {
            "unique_neighbors": unique_neighbors,
            "total_neighbor_hours": round(total_neighbor_hours, 2),
            "total_weighted_hours": round(total_weighted_hours, 2),
            "neighbors_per_hour": round(unique_neighbors / total_hours, 3) if total_hours > 0 else 0,
            "total_hours": round(total_hours, 2),
            "top_neighbors": [
                {
                    "login": l,
                    "hours": round(e["hours"], 2),
                    "weighted_hours": round(e["weighted_hours"], 2),
                    "proximity": e["proximity"],  # "adjacent" | "facing" | "same_row" | "same_cluster"
                    "avatar": avatar_map.get(l),
                }
                for l, e in top_neighbors_raw
            ],
        }

    # 全体統計
    unique_counts = [ps["unique_neighbors"] for ps in per_student.values()]
    print(f"  Unique neighbors: min={min(unique_counts)}, max={max(unique_counts)}, "
          f"mean={statistics.mean(unique_counts):.1f}, median={statistics.median(unique_counts):.1f}")

    # ─── Step 4b: 合格率との相関（KV の piscine_result があれば）─────────────
    # ローカルの summary データまたは KV から piscine_result を取得
    pass_data = {}
    # dev-data.json から合否データを取得（存在する場合のみ）
    dev_data_path = Path(__file__).parent.parent / "public" / "dev-data.json"
    if dev_data_path.exists():
        try:
            with open(dev_data_path) as f:
                dev_data = json.load(f)
            for s in dev_data.get("online", []) + dev_data.get("offline", []):
                login = s.get("login", "")
                result = s.get("piscine_result")
                if login and result:
                    pass_data[login] = 1 if result == "passed" else 0
            print(f"  Pass data loaded: {len(pass_data)} students")
        except Exception as e:
            print(f"  [WARN] Could not load dev-data.json: {e}")

    # 四分位ごとの合格率
    quartile_analysis = None
    correlation = None
    if pass_data:
        # unique_neighbors と pass/fail のペアリスト
        paired = []
        for login in all_logins:
            if login in pass_data and login in per_student:
                paired.append((per_student[login]["unique_neighbors"], pass_data[login]))

        if len(paired) >= 10:
            paired.sort(key=lambda x: x[0])
            q_size = len(paired) // 4
            quartiles = []
            for qi in range(4):
                start = qi * q_size
                end = (qi + 1) * q_size if qi < 3 else len(paired)
                q_data = paired[start:end]
                q_pass_rate = sum(p for _, p in q_data) / len(q_data) if q_data else 0
                q_avg_neighbors = statistics.mean(n for n, _ in q_data) if q_data else 0
                quartiles.append({
                    "quartile": qi + 1,
                    "count": len(q_data),
                    "avg_neighbors": round(q_avg_neighbors, 1),
                    "pass_rate": round(q_pass_rate * 100, 1),
                })
            quartile_analysis = quartiles

            # 点双列相関（簡易版: ピアソン相関で近似）
            n_vals = [n for n, _ in paired]
            p_vals = [p for _, p in paired]
            if len(set(p_vals)) > 1:  # 全員同じ結果でない場合のみ
                n_mean = statistics.mean(n_vals)
                p_mean = statistics.mean(p_vals)
                cov = sum((n - n_mean) * (p - p_mean) for n, p in zip(n_vals, p_vals)) / len(paired)
                n_std = statistics.stdev(n_vals)
                p_std = statistics.stdev(p_vals)
                if n_std > 0 and p_std > 0:
                    correlation = round(cov / (n_std * p_std), 4)

            print(f"  Correlation (neighbors × pass): {correlation}")
            for q in quartiles:
                print(f"    Q{q['quartile']}: avg {q['avg_neighbors']} neighbors → "
                      f"pass rate {q['pass_rate']}% (n={q['count']})")

    # Most connected / Most isolated
    sorted_by_neighbors = sorted(per_student.items(), key=lambda x: -x[1]["unique_neighbors"])
    most_connected = [{"login": l, "unique_neighbors": d["unique_neighbors"]}
                      for l, d in sorted_by_neighbors[:10]]
    most_isolated = [{"login": l, "unique_neighbors": d["unique_neighbors"]}
                     for l, d in sorted_by_neighbors[-10:]]

    print(f"\n  Most connected: {', '.join(m['login'] for m in most_connected[:5])}")
    print(f"  Most isolated:  {', '.join(m['login'] for m in most_isolated[:5])}")

    # ─── Step 5: JSON出力 ────────────────────────────────────────────────────
    print("\n[5] Writing results...")

    result = {
        "piscine_label": PISCINE_LABEL,
        "piscine_start": PISCINE_START.strftime("%Y-%m-%d"),
        "piscine_end": (PISCINE_END - timedelta(days=1)).strftime("%Y-%m-%d"),
        "total_sessions": len(sessions),
        "total_students": len(all_logins),
        "definition": "adjacent: same_row seat=1 (w=1.0) | near: same_row seat=2 (w=0.7) | facing: row_diff=1 seat≤1 (w=0.5) | same_row: seat≥3 (w=0.2) | same_cluster: row_diff≥2 (w=0.1) | overlap≥30min",
        "per_student": per_student,
        "global": {
            "correlation_neighbors_pass": correlation,
            "quartile_pass_rates": quartile_analysis,
            "most_connected": most_connected,
            "most_isolated": most_isolated,
            "stats": {
                "min_neighbors": min(unique_counts),
                "max_neighbors": max(unique_counts),
                "mean_neighbors": round(statistics.mean(unique_counts), 1),
                "median_neighbors": round(statistics.median(unique_counts), 1),
            },
        },
        "computed_at": now.isoformat(),
    }

    # ローカルJSON保存
    out_path = Path(__file__).parent.parent / f"neighbor_analysis_{PISCINE_MONTH}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  Saved to {out_path}")

    # KVアップロード（WORKER_SECRET があれば）
    kv_key = f"neighbors_{PISCINE_MONTH}"
    try:
        upload_to_kv({"type": "neighbors", "key": kv_key, "data": result})
        print(f"  Uploaded to KV as {kv_key}")
    except Exception as e:
        print(f"  [WARN] KV upload failed: {e}")

    print(f"\n=== Done! ===")


if __name__ == "__main__":
    main()
