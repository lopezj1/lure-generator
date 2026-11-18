// Spike #58 screenshots: node shots.mjs (with `vite preview --port 4173` running)
import { chromium } from "playwright";

const OUT = new URL("../screenshots/", import.meta.url).pathname;
const browser = await chromium.launch({
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const page = await browser.newPage({ viewport: { width: 1400, height: 820 } });
const logs = [];
page.on("console", (m) => logs.push(`${m.type()}: ${m.text()}`));
page.on("pageerror", (e) => logs.push(`pageerror: ${e.message}`));

async function load(name) {
  await page.evaluate((n) => { window.ready = false; return window.app.load(n); }, name);
  await page.waitForFunction(() => window.ready === true, null, { timeout: 60000 });
  await page.waitForTimeout(800);
}
async function shot(file) {
  await page.waitForTimeout(600);
  await page.screenshot({ path: OUT + file });
  console.log("saved", file, await page.locator("footer").innerText());
}
const clip = (i, on, v) => page.evaluate(([i, on, v]) => window.app.setClip(i, on, v), [i, on, v]);
const preset = (d, z = 1.6) => page.evaluate(([d, z]) => window.app.preset(d, z), [d, z]);

await page.goto("http://localhost:4173/");
await page.waitForFunction(() => window.ready === true, null, { timeout: 60000 });

for (const m of ["loft3d", "profile25d", "cavity"]) {
  await load(m);
  await clip(0, false, 0);
  await preset("iso");
  await shot(`${m}-iso.png`);
}
// cavity: X cut at mid-length (keeps the nose half; look from +X), Y mid-plane (look from +Y), Z (look from +Z)
await load("cavity");
await clip(0, true, 0); await preset("iso"); await shot("cavity-clipX-iso.png");
await preset("right", 2.5); await shot("cavity-clipX-section.png");
await clip(0, false, 0); await clip(1, true, 0); await preset("rear", 1.2); await shot("cavity-clipY-section.png");
await clip(1, false, 0); await clip(2, true, 0); await preset("top"); await shot("cavity-clipZ-section.png");
await clip(2, true, 0); await clip(0, true, 20); await clip(1, true, 0); await preset("iso"); await shot("cavity-clipXYZ-iso.png");
await clip(0, false, 0); await clip(1, false, 0); await clip(2, false, 0);
for (const m of ["loft3d", "profile25d"]) {
  await load(m);
  await clip(0, true, 0); await preset("right", 2.5); await shot(`${m}-clipX-section.png`);
  await clip(0, false, 0);
}
await load("scene");
await clip(1, true, 0); await preset("rear", 0.9); await shot("scene-clipY-light.png");
await page.evaluate(() => window.app.setTheme("dark")); await shot("scene-clipY-dark.png");
await page.evaluate(() => window.app.setTheme("light"));
await clip(1, false, 0);
await page.evaluate(() => window.viewer.setActiveTab("tree"));
// tree toggling: hide the profile25d part
await page.evaluate(() => window.viewer.setState("/Group/profile25d", [0, 0]));
await preset("iso"); await shot("scene-tree-hidden-profile25d.png");
console.log(logs.filter((l) => !l.startsWith("debug")).slice(0, 40).join("\n"));
await browser.close();
