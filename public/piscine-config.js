/**
 * 全Piscine日程の一元管理
 *
 * フロントエンド各ページ (compare.html, stats.html, dashboard.html 等) から
 * <script src="piscine-config.js"></script> で読み込んで使用する。
 *
 * 新しい月を追加する場合はここにエントリを追加するだけでOK。
 * バックエンド (fetch_data.py の _PISCINE_CONFIG、workers/index.js の VALID_MONTHS) は
 * 別途更新が必要。
 */
const PISCINE_MONTHS = {
  '2408': { label: '8月ピシン',  start: '2024-08-05', end: '2024-08-30', year: 2024 },
  '2409': { label: '9月ピシン',  start: '2024-09-02', end: '2024-09-27', year: 2024 },
  '2502': { label: '2月ピシン',  start: '2025-02-03', end: '2025-02-28', year: 2025 },
  '2503': { label: '3月ピシン',  start: '2025-03-11', end: '2025-04-05', year: 2025 },
  '02':   { label: '2月ピシン',  start: '2026-02-02', end: '2026-02-27', year: 2026 },
  '03':   { label: '3月ピシン',  start: '2026-03-16', end: '2026-04-10', year: 2026 },
};

/**
 * 月コードから表示用の期間文字列を生成
 * @param {string} code - 月コード (例: '02', '2408')
 * @returns {string} 例: 'Feb 2 – Feb 27, 2026'
 */
function formatPiscinePeriod(code) {
  const m = PISCINE_MONTHS[code];
  if (!m) return '';
  const s = new Date(m.start + 'T00:00:00');
  const e = new Date(m.end + 'T00:00:00');
  const fmt = (d) => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  return `${fmt(s)} – ${fmt(e)}, ${m.year}`;
}

/**
 * 月コードから表示用ラベル + 期間を生成
 * @param {string} code - 月コード
 * @returns {string} 例: '8月ピシン (Aug 5 - Aug 30, 2024)'
 */
function formatPiscineLabel(code) {
  const m = PISCINE_MONTHS[code];
  if (!m) return code;
  return `${m.label} (${formatPiscinePeriod(code)})`;
}
