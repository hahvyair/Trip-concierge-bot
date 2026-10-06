// node render_png.js <in.html> <out.png> [width]
// Screenshots the #viz element of a self-contained HTML page at 2x for Telegram.
// Uses the preinstalled Chromium; run with NODE_PATH=$(npm root -g).
const { chromium } = require("playwright");
const fs = require("fs");
(async () => {
  const [src, out, width] = process.argv.slice(2);
  const exe = ["/opt/pw-browsers/chromium-1194/chrome-linux/chrome", "/opt/pw-browsers/chromium"].find((p) => fs.existsSync(p));
  const b = await chromium.launch(exe ? { executablePath: exe } : {});
  const p = await b.newPage({ viewport: { width: Number(width || 900), height: 800 }, deviceScaleFactor: 2 });
  await p.setContent(fs.readFileSync(src, "utf8"), { waitUntil: "load" });
  await (await p.$("#viz")).screenshot({ path: out });
  await b.close();
  console.log("png ok");
})().catch((e) => { console.error(e.message); process.exit(1); });
