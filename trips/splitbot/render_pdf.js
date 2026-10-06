// node render_pdf.js <in.html> <out.pdf>
// Prints a self-contained HTML page to PDF; page sizes and margins come from
// the page's own @page rules. Uses the preinstalled Chromium; run with
// NODE_PATH=$(npm root -g).
const { chromium } = require("playwright");
const fs = require("fs");
(async () => {
  const [src, out] = process.argv.slice(2);
  const exe = ["/opt/pw-browsers/chromium-1194/chrome-linux/chrome", "/opt/pw-browsers/chromium"].find((p) => fs.existsSync(p));
  const b = await chromium.launch(exe ? { executablePath: exe } : {});
  const p = await b.newPage();
  await p.setContent(fs.readFileSync(src, "utf8"), { waitUntil: "load" });
  await p.pdf({ path: out, printBackground: true, preferCSSPageSize: true });
  await b.close();
  console.log("pdf ok");
})().catch((e) => { console.error(e.message); process.exit(1); });
