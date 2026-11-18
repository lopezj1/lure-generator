// Spike #58: caps on ocp-tessellate's plain BRepMesh output (tips not watertight)
import { chromium } from "playwright";
const OUT = new URL("../screenshots/", import.meta.url).pathname;
const browser = await chromium.launch({ args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage({ viewport: { width: 1400, height: 820 } });
await page.goto("http://localhost:4173/");
await page.waitForFunction(() => window.ready === true, null, { timeout: 60000 });
for (const [m, i, view, z, off] of [["loft3d_brepmesh", 0, "right", 2.5, 0], ["loft3d_brepmesh", 0, "right", 6, -80], ["cavity_brepmesh", 1, "rear", 1.2, 0], ["loft3d", 0, "right", 6, -80]]) {
  await page.evaluate((n) => { window.ready = false; return window.app.load(n); }, m);
  await page.waitForFunction(() => window.ready === true);
  for (const k of [0, 1, 2]) await page.evaluate((k) => window.app.setClip(k, false, 0), k);
  await page.evaluate(([i, off]) => window.app.setClip(i, true, off), [i, off]);
  await page.evaluate(([v, z]) => window.app.preset(v, z), [view, z]);
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${OUT}${m}-clip${"XYZ"[i]}${off ? "-near-tip" : ""}.png` });
  console.log("saved", m, await page.locator("footer").innerText());
}
await browser.close();
