#!/usr/bin/env python3
"""42 Piscine Commitment Tracker - Backend Server"""

import json
import os
import re
import time
import threading
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests

# --- Configuration ---
INTRA_API_BASE = "https://api.intra.42.fr"
TOKEN_URL = f"{INTRA_API_BASE}/oauth/token"

# Piscine February 2026 dates (JST)
JST = timezone(timedelta(hours=9))
PISCINE_START = datetime(2026, 2, 2, 0, 0, 0, tzinfo=JST)
PISCINE_END = datetime(2026, 2, 28, 0, 0, 0, tzinfo=JST)  # end of Feb 27
PISCINE_DAYS = 26  # Feb 2 (Mon) - Feb 27 (Fri)
TARGET_HOURS_PER_DAY = 8

# Campus ID for 42 Tokyo
CAMPUS_ID = 26
PISCINE_CURSUS_ID = 9

# Token cache
_token_cache = {"token": None, "expires_at": 0}

# Dashboard cache
_cache = {
    "piscine_students": {"data": None, "expires_at": 0},
    "active_locations": {"data": None, "expires_at": 0},
    "location_hours": {"data": None, "expires_at": 0},
}
PISCINE_STUDENTS_TTL = 3600  # 1 hour
ACTIVE_LOCATIONS_TTL = 60    # 1 minute
LOCATION_HOURS_TTL = 300     # 5 minutes

# Background fetch state
_hours_fetch_lock = threading.Lock()
_hours_fetch_running = False


def load_env():
    """Load .env file"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip()


def get_access_token():
    """Get OAuth2 access token using client credentials flow"""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    client_id = os.environ.get("CLIENT_ID", "")
    client_secret = os.environ.get("CLIENT_SECRET", "")

    if not client_id or not client_secret:
        raise Exception("CLIENT_ID and CLIENT_SECRET must be set in .env file")

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    })
    resp.raise_for_status()
    data = resp.json()

    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 7200)
    return _token_cache["token"]


def fetch_location_stats(login):
    """Fetch daily location stats using /v2/users/:login/locations_stats"""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    params = {
        "begin_at": PISCINE_START.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "end_at": PISCINE_END.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }

    resp = requests.get(
        f"{INTRA_API_BASE}/v2/users/{login}/locations_stats",
        headers=headers,
        params=params,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"[DEBUG] locations_stats for {login}: {len(data)} days found")
    if data:
        sample = dict(list(data.items())[:3])
        print(f"[DEBUG] Sample: {sample}")
    return data


def fetch_active_location(login):
    """Check if the user is currently logged in by fetching active locations"""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(
        f"{INTRA_API_BASE}/v2/users/{login}/locations",
        headers=headers,
        params={"filter[active]": "true", "page[size]": 1},
    )
    resp.raise_for_status()
    data = resp.json()

    if data and data[0].get("end_at") is None:
        begin = data[0]["begin_at"].replace("Z", "+00:00")
        return datetime.fromisoformat(begin)
    return None


def get_cached(key, fetch_fn, ttl):
    """Generic TTL cache getter"""
    now = time.time()
    if _cache[key]["data"] is not None and _cache[key]["expires_at"] > now:
        return _cache[key]["data"]
    data = fetch_fn()
    _cache[key]["data"] = data
    _cache[key]["expires_at"] = now + ttl
    return data


def parse_host(host):
    """Parse seat host like 'c1r1s1.42tokyo.jp' into (cluster, seat)"""
    if not host:
        return None, None
    name = host.split(".")[0]
    match = re.match(r"^(c\d+)(.*)", name)
    if match:
        return match.group(1), match.group(2)
    return None, None


def fetch_all_piscine_students():
    """Fetch all C Piscine students at campus 26 with pagination"""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    all_students = []
    page = 1

    while True:
        resp = requests.get(
            f"{INTRA_API_BASE}/v2/cursus/{PISCINE_CURSUS_ID}/cursus_users",
            headers=headers,
            params={
                "filter[campus_id]": CAMPUS_ID,
                "page[size]": 100,
                "page[number]": page,
                "range[begin_at]": f"{PISCINE_START.strftime('%Y-%m-%d')},{PISCINE_END.strftime('%Y-%m-%d')}",
                "sort": "user_id",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        for item in data:
            user = item.get("user", {})
            image = (user.get("image") or {})
            image_small = image.get("versions", {}).get("small", "") if image else ""
            all_students.append({
                "login": user.get("login", ""),
                "display_name": user.get("usual_full_name") or user.get("displayname", ""),
                "image_small": image_small or "",
            })

        print(f"[DEBUG] Piscine students page {page}: {len(data)} results")
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.5)

    print(f"[DEBUG] Total piscine students: {len(all_students)}")
    return all_students


def fetch_all_active_locations():
    """Fetch all active locations at campus 26 with pagination"""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    all_locations = []
    page = 1

    while True:
        resp = requests.get(
            f"{INTRA_API_BASE}/v2/campus/{CAMPUS_ID}/locations",
            headers=headers,
            params={
                "filter[active]": "true",
                "page[size]": 100,
                "page[number]": page,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        for loc in data:
            user = loc.get("user", {})
            host = loc.get("host", "")
            cluster, seat = parse_host(host)
            image = (user.get("image") or {})
            image_small = image.get("versions", {}).get("small", "") if image else ""
            all_locations.append({
                "login": user.get("login", ""),
                "display_name": user.get("usual_full_name") or user.get("displayname", ""),
                "image_small": image_small or "",
                "host": host,
                "cluster": cluster,
                "seat": seat,
                "begin_at": loc.get("begin_at", ""),
            })

        print(f"[DEBUG] Active locations page {page}: {len(data)} results")
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.5)

    return all_locations


def fetch_all_location_hours():
    """Fetch location_stats for each piscine student to compute total hours.

    Uses per-user /v2/users/:login/locations_stats endpoint.
    With 147 students at 0.5s interval, takes ~75s on first call.
    Cached for 5 minutes after that.
    """
    # Get piscine students first (uses its own cache)
    students = get_cached("piscine_students", fetch_all_piscine_students, PISCINE_STUDENTS_TTL)

    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    user_hours = {}
    params = {
        "begin_at": PISCINE_START.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "end_at": PISCINE_END.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }

    total = len(students)
    for i, student in enumerate(students):
        login = student["login"]
        try:
            resp = requests.get(
                f"{INTRA_API_BASE}/v2/users/{login}/locations_stats",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            total_h = 0.0
            for date_str, duration_str in data.items():
                total_h += parse_duration(duration_str)
            user_hours[login] = round(total_h, 2)
        except Exception as e:
            print(f"[WARN] Failed to fetch hours for {login}: {e}")
            user_hours[login] = 0

        if (i + 1) % 20 == 0 or (i + 1) == total:
            print(f"[DEBUG] Location hours: {i + 1}/{total} students fetched")
        time.sleep(0.5)  # Rate limit: 2 req/sec

    print(f"[DEBUG] Location hours complete: {len(user_hours)} students")
    return user_hours


def _trigger_hours_fetch_bg():
    """Start background thread to fetch location hours if not already running"""
    global _hours_fetch_running
    with _hours_fetch_lock:
        if _hours_fetch_running:
            return
        _hours_fetch_running = True

    def _do_fetch():
        global _hours_fetch_running
        try:
            print("[DEBUG] Background hours fetch started...")
            data = fetch_all_location_hours()
            _cache["location_hours"]["data"] = data
            _cache["location_hours"]["expires_at"] = time.time() + LOCATION_HOURS_TTL
            print("[DEBUG] Background hours fetch complete!")
        except Exception as e:
            print(f"[ERROR] Background hours fetch failed: {e}")
        finally:
            with _hours_fetch_lock:
                _hours_fetch_running = False

    t = threading.Thread(target=_do_fetch, daemon=True)
    t.start()


def parse_duration(duration_str):
    """Parse 'HH:MM:SS.microseconds' to hours (float)"""
    if not duration_str:
        return 0.0
    parts = duration_str.split(":")
    if len(parts) != 3:
        return 0.0
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    return hours + minutes / 60 + seconds / 3600


def calculate_metrics(stats, active_since, login):
    """Calculate piscine commitment metrics from location_stats data"""
    now = datetime.now(JST)

    # Parse daily hours from stats
    daily_hours = {}
    for date_str, duration_str in stats.items():
        hours = parse_duration(duration_str)
        if hours > 0:
            daily_hours[date_str] = hours

    total_hours = sum(daily_hours.values())

    # Add currently active session time (not yet reflected in stats)
    active_extra_hours = 0
    if active_since:
        active_since_jst = active_since.astimezone(JST)
        active_extra_hours = max(0, (now - active_since_jst).total_seconds() / 3600)
        today_str = now.strftime("%Y-%m-%d")
        # The active session may partially be included in stats already,
        # but for "right now" display we add the extra time
        total_hours += active_extra_hours

    total_target = TARGET_HOURS_PER_DAY * PISCINE_DAYS

    # Elapsed time calculation (proportional, including partial current day)
    if now < PISCINE_START:
        elapsed_hours = 0
        elapsed_days = 0
    elif now > PISCINE_END:
        elapsed_hours = (PISCINE_END - PISCINE_START).total_seconds() / 3600
        elapsed_days = PISCINE_DAYS
    else:
        elapsed_hours = (now - PISCINE_START).total_seconds() / 3600
        elapsed_days = elapsed_hours / 24

    # Average hours per day (proportional to elapsed time)
    avg_hours_per_day = total_hours / elapsed_days if elapsed_days > 0 else 0

    # Remaining calculations
    remaining_hours_total = max(0, total_target - total_hours)

    if now >= PISCINE_END:
        remaining_days = 0
        required_avg = 0
    else:
        remaining_seconds = (PISCINE_END - max(now, PISCINE_START)).total_seconds()
        remaining_days = remaining_seconds / 86400
        required_avg = remaining_hours_total / remaining_days if remaining_days > 0 else 0

    # Progress
    progress_pct = min(100, (total_hours / total_target) * 100) if total_target > 0 else 0

    # On track? (expected hours based on proportional elapsed time)
    expected_hours = (elapsed_hours / 24) * TARGET_HOURS_PER_DAY if elapsed_hours > 0 else 0
    diff_from_target = total_hours - expected_hours

    # Build daily breakdown
    daily_data = []
    d = PISCINE_START
    while d < PISCINE_END:
        day_key = d.strftime("%Y-%m-%d")
        hours = round(daily_hours.get(day_key, 0), 2)
        daily_data.append({
            "date": day_key,
            "weekday": d.strftime("%a"),
            "hours": hours,
            "met_target": hours >= TARGET_HOURS_PER_DAY,
        })
        d += timedelta(days=1)

    return {
        "login": login,
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
        "remaining_hours_needed": round(remaining_hours_total, 2),
        "required_avg_remaining": round(required_avg, 2),
        "progress_pct": round(progress_pct, 1),
        "diff_from_target": round(diff_from_target, 2),
        "on_track": diff_from_target >= 0,
        "is_active": active_since is not None,
        "active_extra_hours": round(active_extra_hours, 2),
        "daily": daily_data,
        "updated_at": now.isoformat(),
    }


class RequestHandler(SimpleHTTPRequestHandler):
    """HTTP request handler"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="public", **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/metrics":
            self.handle_metrics(parsed)
        elif parsed.path == "/api/dashboard":
            self.handle_dashboard()
        elif parsed.path == "/api/piscine_students":
            self.handle_piscine_students()
        elif parsed.path == "/api/active_locations":
            self.handle_active_locations(parsed)
        elif parsed.path == "/api/health":
            self.send_json({"status": "ok"})
        else:
            super().do_GET()

    def handle_metrics(self, parsed):
        qs = parse_qs(parsed.query)
        login = qs.get("login", ["yuichika"])[0]

        try:
            stats = fetch_location_stats(login)
            active_since = fetch_active_location(login)
            metrics = calculate_metrics(stats, active_since, login)
            self.send_json(metrics)
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def handle_piscine_students(self):
        try:
            students = get_cached("piscine_students", fetch_all_piscine_students, PISCINE_STUDENTS_TTL)
            self.send_json({
                "students": students,
                "total": len(students),
                "cached_at": datetime.now(JST).isoformat(),
            })
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def handle_active_locations(self, parsed):
        try:
            locations = get_cached("active_locations", fetch_all_active_locations, ACTIVE_LOCATIONS_TTL)
            qs = parse_qs(parsed.query)
            cluster = qs.get("cluster", [None])[0]
            if cluster:
                locations = [loc for loc in locations if loc["cluster"] == cluster]
            self.send_json({
                "locations": locations,
                "total": len(locations),
                "cached_at": datetime.now(JST).isoformat(),
            })
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def handle_dashboard(self):
        try:
            students = get_cached("piscine_students", fetch_all_piscine_students, PISCINE_STUDENTS_TTL)
            locations = get_cached("active_locations", fetch_all_active_locations, ACTIVE_LOCATIONS_TTL)

            # Hours data: use cache if available, otherwise return without blocking
            now_t = time.time()
            hours_loading = False
            if _cache["location_hours"]["data"] is not None:
                hours_map = _cache["location_hours"]["data"]
                # Trigger background refresh if expired
                if _cache["location_hours"]["expires_at"] <= now_t:
                    _trigger_hours_fetch_bg()
            else:
                # No data yet - return empty and trigger background fetch
                hours_map = {}
                hours_loading = True
                _trigger_hours_fetch_bg()

            student_set = {s["login"] for s in students}
            student_map = {s["login"]: s for s in students}
            online_logins = set()
            online = []

            for loc in locations:
                login = loc["login"]
                if login in student_set:
                    entry = {**student_map[login], **loc}
                    entry["total_hours"] = hours_map.get(login, 0)
                    online.append(entry)
                    online_logins.add(login)

            offline = []
            for s in students:
                if s["login"] not in online_logins:
                    entry = {**s, "total_hours": hours_map.get(s["login"], 0)}
                    offline.append(entry)

            # Default sort: by total_hours descending
            online.sort(key=lambda x: x.get("total_hours", 0), reverse=True)
            offline.sort(key=lambda x: x.get("total_hours", 0), reverse=True)

            self.send_json({
                "online": online,
                "offline": offline,
                "total_students": len(students),
                "total_online": len(online),
                "hours_loading": hours_loading,
                "cached_at": datetime.now(JST).isoformat(),
            })
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {format % args}")


def _warmup_cache():
    """Prefetch all caches on startup so the first request is fast."""
    def _run():
        print("[WARMUP] Starting cache prefetch...")
        try:
            get_cached("piscine_students", fetch_all_piscine_students, PISCINE_STUDENTS_TTL)
            print("[WARMUP] piscine_students ready")
        except Exception as e:
            print(f"[WARMUP] piscine_students failed: {e}")
        try:
            get_cached("active_locations", fetch_all_active_locations, ACTIVE_LOCATIONS_TTL)
            print("[WARMUP] active_locations ready")
        except Exception as e:
            print(f"[WARMUP] active_locations failed: {e}")
        # hours fetch runs in its own bg thread (takes ~75s)
        _trigger_hours_fetch_bg()
        print("[WARMUP] hours fetch started in background")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _start_auto_refresh():
    """Background thread that periodically refreshes caches without waiting for requests."""
    def _run():
        while True:
            # Refresh active_locations every 60s
            time.sleep(60)
            try:
                data = fetch_all_active_locations()
                _cache["active_locations"]["data"] = data
                _cache["active_locations"]["expires_at"] = time.time() + ACTIVE_LOCATIONS_TTL
            except Exception as e:
                print(f"[AUTO-REFRESH] active_locations failed: {e}")

            # Refresh location_hours every 5 min (aligned with TTL)
            now = time.time()
            if _cache["location_hours"]["data"] is None or _cache["location_hours"]["expires_at"] <= now:
                _trigger_hours_fetch_bg()

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def main():
    load_env()
    port = int(os.environ.get("PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), RequestHandler)
    print(f"Server running at http://localhost:{port}")
    print(f"Piscine period: {PISCINE_START.strftime('%Y-%m-%d')} - {(PISCINE_END - timedelta(days=1)).strftime('%Y-%m-%d')} ({PISCINE_DAYS} days)")
    print(f"Target: {TARGET_HOURS_PER_DAY}h/day = {TARGET_HOURS_PER_DAY * PISCINE_DAYS}h total")
    _warmup_cache()
    _start_auto_refresh()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
