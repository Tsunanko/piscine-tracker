#!/usr/bin/env python3
"""Piscine 期間の一元定義（Python側）。

fetch_data.py と fetch_neighbors.py の両方がここを読む。

以前は同じ定義が2つのスクリプトに別々に書かれていて、片方だけ更新した結果
「データは取れたのに座席隣接分析だけ落ちる」という事故が起きた（2026-08-11）。
Piscine を追加するときに触る場所は、これで次の3つになる:

  1. このファイル（バックエンド）
  2. public/piscine-config.js  の PISCINE_MONTHS（フロントエンド）
  3. workers/index.js          の VALID_MONTHS（保存先の月キー検証）

※ 2 を忘れると画面にタブが出ず、3 を忘れると **データが 02 のキーに上書き保存される**。
"""

from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

# "end" は「最終日の翌日」を入れる（期間の判定を start <= t < end で書けるようにするため）
PISCINE_CONFIG = {
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
        "end":   datetime(2026, 2, 28, 0, 0, 0, tzinfo=JST),  # 2/27の翌日
        "days":  26,
    },
    "03": {
        "start": datetime(2026, 3, 16, 0, 0, 0, tzinfo=JST),
        "end":   datetime(2026, 4, 11, 0, 0, 0, tzinfo=JST),  # 4/10の翌日
        "days":  26,
    },
    "2607": {
        "start": datetime(2026, 7, 27, 0, 0, 0, tzinfo=JST),
        "end":   datetime(2026, 8, 22, 0, 0, 0, tzinfo=JST),  # 8/21の翌日
        "days":  26,
    },
}


def get_config(month):
    """月コードの設定を返す。未対応なら、対応済みの一覧を添えて落とす。"""
    if month not in PISCINE_CONFIG:
        raise ValueError(
            f"Unsupported PISCINE_MONTH: {month}. "
            f"Use one of: {', '.join(PISCINE_CONFIG.keys())}. "
            f"（新しい期を足すときは scripts/piscine_months.py に追記する）"
        )
    return PISCINE_CONFIG[month]


def get_label(month):
    """ログ表示用のラベルを返す（例: '2026-07 Piscine'）。"""
    return get_config(month)["start"].strftime("%Y-%m") + " Piscine"
