/**
 * BSE SENSEX/BANKEX bhavcopy bulk downloader — RUN THIS ON YOUR OWN MACHINE.
 *
 * BSE is Akamai bot-gated, so it can't be fetched from Claude's sandbox or the VPS.
 * A REAL browser on your desktop passes the challenge. This drives real Chromium
 * to download every BSE F&O (equity-derivatives) UDiFF bhavcopy CSV in a date
 * range to ./bse_csv/. BSE now serves these as a PLAIN .CSV (not zipped), at:
 *   https://www.bseindia.com/download/BhavCopy/Derivative/BhavCopy_BSE_FO_0_0_0_<YYYYMMDD>_F_0000.CSV
 *
 * SETUP (one time, in an empty folder):
 *   npm init -y
 *   npm install playwright
 *   npx playwright install chromium
 *   (copy this file into that folder)
 *
 * RUN:
 *   node bse_bhavcopy_download.js 2024-01-01 2025-12-31
 *
 * A Chrome window opens (leave it alone). Files save to ./bse_csv/. Holidays and
 * weekends are skipped. Safe to re-run — it skips files you already have.
 * THEN ingest:  python bhavcopy_ingest.py --source bse --from-zips ./bse_csv
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const OUT = path.join(process.cwd(), 'bse_csv');
const BASE = 'https://www.bseindia.com/download/BhavCopy/Derivative/';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

function* dates(start, end) {
  const d = new Date(start), last = new Date(end);
  while (d <= last) { const w = d.getDay(); if (w !== 0 && w !== 6) yield new Date(d); d.setDate(d.getDate() + 1); }
}
const ymd = (d) => `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;

(async () => {
  const [, , start, end] = process.argv;
  if (!start || !end) { console.error('usage: node bse_bhavcopy_download.js YYYY-MM-DD YYYY-MM-DD'); process.exit(1); }
  fs.mkdirSync(OUT, { recursive: true });

  const browser = await chromium.launch({ headless: false });
  const ctx = await browser.newContext({ userAgent: UA });
  const page = await ctx.newPage();
  console.log('warming BSE session…');
  try { await page.goto('https://www.bseindia.com/markets/marketinfo/bhavcopy', { waitUntil: 'networkidle', timeout: 90000 }); } catch (e) {}
  await page.waitForTimeout(6000);

  let ok = 0, skip = 0, miss = 0;
  for (const dt of dates(start, end)) {
    const dd = ymd(dt);
    const dest = path.join(OUT, `BhavCopy_BSE_FO_0_0_0_${dd}_F_0000.CSV`);
    if (fs.existsSync(dest) && fs.statSync(dest).size > 5000) { skip++; continue; }
    const url = `${BASE}BhavCopy_BSE_FO_0_0_0_${dd}_F_0000.CSV`;
    const res = await page.evaluate(async (u) => {
      try {
        const r = await fetch(u, { headers: { 'Accept': '*/*' } });
        const t = await r.text();
        return { status: r.status, head: t.slice(0, 6), len: t.length, body: t };
      } catch (e) { return { error: String(e) }; }
    }, url);
    // real file starts with the header "TradDt"; the 12.5KB HTML shell starts with "<"
    if (res.body && res.head.startsWith('Trad') && res.len > 5000) {
      fs.writeFileSync(dest, res.body, 'utf8');
      ok++; process.stdout.write(`  ${dd} ok (${res.len} B)\n`);
    } else {
      miss++; process.stdout.write(`  ${dd} holiday/none\n`);
    }
    await page.waitForTimeout(350);
  }
  console.log(`\nDONE. downloaded=${ok} already_had=${skip} missing/holiday=${miss}`);
  console.log(`files in: ${OUT}`);
  console.log(`next:  python bhavcopy_ingest.py --source bse --from-zips "${OUT}"`);
  await browser.close();
})();
