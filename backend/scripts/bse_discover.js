/**
 * BSE download-URL prober — tries known filenames across candidate folders for a
 * recent trading day and reports which combination returns a real file.
 * RUN (in the SensexData folder):  node bse_discover.js
 */
const { chromium } = require('playwright');
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

const D = '20260703';          // YYYYMMDD (a recent trading day known to have data)
const DDMMYYYY = '03072026';

const files = [
  `BhavCopy_BSE_FO_0_0_0_${D}_F_0000.CSV.zip`,
  `BhavCopy_BSE_FO_0_0_0_${D}_F_0000.CSV`,
  `BSE_EQD_BHAVCOPY_${DDMMYYYY}.zip`,
  `bhavcopy${DDMMYYYY.slice(0,2)}-${DDMMYYYY.slice(2,4)}-${DDMMYYYY.slice(6,8)}.zip`, // bhavcopy03-07-26.zip
];
const bases = [
  'https://www.bseindia.com/download/BhavCopy/Derivative/',
  'https://www.bseindia.com/downloads1/',
  'https://www.bseindia.com/download/Bhavcopy/Derivative/',
  'https://www.bseindia.com/BSEDATA/gross/2026/',
  'https://www.bseindia.com/download/BhavCopy/Equity/',
];

(async () => {
  const browser = await chromium.launch({ headless: false });
  const ctx = await browser.newContext({ userAgent: UA });
  const page = await ctx.newPage();
  console.log('warming BSE (leave Chrome alone)…');
  try { await page.goto('https://www.bseindia.com/markets/marketinfo/bhavcopy', { waitUntil: 'networkidle', timeout: 90000 }); } catch (e) {}
  await page.waitForTimeout(5000);

  console.log('\n================ COPY EVERYTHING BELOW AND PASTE TO CLAUDE ================');
  for (const base of bases) {
    for (const f of files) {
      const url = base + f;
      const out = await page.evaluate(async (u) => {
        try {
          const r = await fetch(u, { headers: { 'Accept': '*/*' } });
          const b = new Uint8Array(await r.arrayBuffer());
          const head = Array.from(b.slice(0, 4)).map(x => x.toString(16).padStart(2, '0')).join('');
          return { s: r.status, n: b.length, head };
        } catch (e) { return { err: String(e).slice(0, 40) }; }
      }, url);
      const flag = out.head === '504b0304' ? '  <== ZIP! ' : (out.n > 5000 && out.s === 200 ? '  <== file? ' : '');
      console.log(`${out.s || 'ERR'}  ${String(out.n || 0).padStart(8)}  ${out.head || out.err || ''}${flag}  ${url}`);
    }
  }
  console.log('================ END ================\n');
  await browser.close();
})();
