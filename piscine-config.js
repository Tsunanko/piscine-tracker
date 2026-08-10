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
  '2303': { label: '3月ピシン',  start: '2023-03-06', end: '2023-03-31', year: 2023 },
  '2408': { label: '8月ピシン',  start: '2024-08-05', end: '2024-08-30', year: 2024 },
  '2409': { label: '9月ピシン',  start: '2024-09-02', end: '2024-09-27', year: 2024 },
  '2502': { label: '2月ピシン',  start: '2025-02-03', end: '2025-02-28', year: 2025 },
  '2503': { label: '3月ピシン',  start: '2025-03-11', end: '2025-04-05', year: 2025 },
  '02':   { label: '2月ピシン',  start: '2026-02-02', end: '2026-02-27', year: 2026 },
  '03':   { label: '3月ピシン',  start: '2026-03-16', end: '2026-04-10', year: 2026 },
  '2607': { label: '7月ピシン',  start: '2026-07-27', end: '2026-08-21', year: 2026 },
};

/**
 * 「いま見るべき月コード」を返す。
 *
 * 各ページの初期表示に使う。優先順位:
 *   1. 開催中の期（今日が start〜end の中にある）
 *   2. すでに始まった期のうち最も新しいもの（＝直近に終わった期）
 *   3. どれにも当てはまらなければ定義の先頭
 *
 * 日付は 'YYYY-MM-DD' の文字列比較で判定する（この形式は辞書順＝時系列順）。
 *
 * @param {Date} [today] - 判定基準日。省略時は現在時刻（テスト時に差し込める）
 * @returns {string} 月コード (例: '2607')
 */
function getActivePiscineMonth(today) {
  const d = today || new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const ymd = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const codes = Object.keys(PISCINE_MONTHS);
  if (!codes.length) return '';

  const ongoing = codes.find((c) => PISCINE_MONTHS[c].start <= ymd && ymd <= PISCINE_MONTHS[c].end);
  if (ongoing) return ongoing;

  const started = codes
    .filter((c) => PISCINE_MONTHS[c].start <= ymd)
    .sort((a, b) => PISCINE_MONTHS[a].start.localeCompare(PISCINE_MONTHS[b].start));
  return started.length ? started[started.length - 1] : codes[0];
}

/**
 * ページ間リンクに付ける month クエリを生成する。
 *
 * 以前は「既定月（02）のときだけ省略する」書き方が各ページに散っていたため、
 * 既定月を変えるとリンクが壊れた。常に付ける方式にして、その依存をなくす。
 *
 * @param {string} code - 月コード
 * @param {string} [sep] - 区切り文字。URLの先頭なら '?'（既定）、既にクエリがあるなら '&'
 * @returns {string} 例: '?month=2607' / '&month=2607'（code が空なら空文字）
 */
function monthQuery(code, sep) {
  if (!code) return '';
  return `${sep || '?'}month=${encodeURIComponent(code)}`;
}

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
