/**
 * BSE SENSEX/BANKEX bhavcopy bulk downloader — RUN THIS ON YOUR OWN MACHINE.
 *
 * BSE is Akamai bot-gated, so it can't be fetched from Claude's sandbox or the VPS.
 * But a REAL browser on your desktop passes the challenge. This script drives a
 * real Chromium to download every BSE F&O bhavcopy zip in a date range to ./bse_zips/.
 * Then you feed those zips to the ingest with:  python bhavcopy_ingest.py --source bse --from-zips ./bse_zips
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
 * A Chrome window opens (leave it alone). It saves zips to ./bse_zips/.
 * Holidays/weekends are skipped automatically. Safe to re-run — it skips files
 * you already have. If a day fails, re-run and it only fetches the missing ones.
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const OUT = path.join(process.cwd(), 'bse_zips');
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

function* dates(start, end) {
  const d = new Date(start);
  const last = new Date(end);
  while (d <= last) {
    const dow = d.getDay();
    if (dow !== 0 && dow !== 6) yield new Date(d);  // skip Sat/Sun
    d.setDate(d.getDate() + 1);
  }
}
const ymd = (d) => `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;

(async () => {
  const [, , start, end] = process.argv;
  if (!start || !end) { console.error('usage: node bse_bhavcopy_download.js YYYY-MM-DD YYYY-MM-DD'); process.exit(1); }
  fs.mkdirSync(OUT, { recursive: true });

  const browser = await chromium.launch({ headless: false });   // real visible browser
  const ctx = await browser.newContext({ userAgent: UA, acceptDownloads: true });
  const page = await ctx.newPage();
  console.log('warming BSE session (solving Akamai)…');
  await page.goto('https://www.bseindia.com/markets/Derivatives/DeriReports.aspx', { waitUntil: 'networkidle', timeout: 90000 });
  await page.waitForTimeout(6000);

  let ok = 0, skip = 0, miss = 0;
  for (const d of dates(start, end)) {
    const dd = ymd(d);
    const dest = path.join(OUT, `BhavCopy_BSE_FO_0_0_0_${dd}_F_0000.CSV.zip`);
    if (fs.existsSync(dest) && fs.statSync(dest).size > 500) { skip++; continue; }
    const url = `https://www.bseindia.com/download/BhavCopy/Derivative/BhavCopy_BSE_FO_0_0_0_${dd}_F_0000.CSV.zip`;
    try {
      const [download] = await Promise.all([
        page.waitForEvent('download', { timeout: 20000 }),
        page.goto(url).catch(() => {}),   // navigation becomes a download; ignore the goto abort
      ]);
      await download.saveAs(dest);
      const size = fs.statSync(dest).size;
      if (size < 500) { fs.unlinkSync(dest); miss++; process.stdout.write(`  ${dd} holiday/empty\n`); }
      else { ok++; process.stdout.write(`  ${dd} ok (${size} B)\n`); }
    } catch (e) {
      miss++; process.stdout.write(`  ${dd} no file (holiday or gated) \n`);
    }
    await page.waitForTimeout(400);   // be polite
  }
  console.log(`\nDONE. downloaded=${ok} already_had=${skip} missing/holiday=${miss}`);
  console.log(`zips in: ${OUT}`);
  console.log(`next:  python bhavcopy_ingest.py --source bse --from-zips "${OUT}"`);
  await browser.close();
})();
