// Spike #58: exercise the built-in measure tools (mesh backend) and log what they report.
import { chromium } from "playwright";
const OUT = new URL("../screenshots/", import.meta.url).pathname;
const browser = await chromium.launch({ args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage({ viewport: { width: 1400, height: 820 } });
const logs = [];
page.on("console", (m) => logs.push(`${m.type()}: ${m.text()}`));
page.on("pageerror", (e) => logs.push(`pageerror: ${e.message}`));
await page.goto("http://localhost:4173/");
await page.waitForFunction(() => window.ready === true, null, { timeout: 60000 });
await page.evaluate(() => { window.ready = false; return window.app.load("profile25d"); });
await page.waitForFunction(() => window.ready === true);
await page.evaluate(() => window.app.setClip(0, false, 0));
await page.evaluate(() => window.app.preset("iso", 1));
await page.waitForTimeout(800);

await page.evaluate(() => window.viewer.activateTool("properties", true));
await page.waitForTimeout(300);
await page.mouse.click(600, 400);
await page.waitForTimeout(1200);
await page.screenshot({ path: OUT + "measure-properties-flat-face.png" });
console.log("properties:", (await page.locator(".tcv_properties_measurement_panel").innerText().catch(() => "n/a")).replace(/\s+/g, " "));
await page.evaluate(() => { window.ready = false; return window.app.load("profile25d"); });
await page.waitForFunction(() => window.ready === true);
await page.evaluate(() => window.app.setClip(0, false, 0));
await page.evaluate(() => window.app.preset("iso", 1));
await page.waitForTimeout(800);

await page.mouse.click(580, 56);
await page.waitForTimeout(300);
await page.mouse.move(550, 370); await page.mouse.move(560, 380, { steps: 5 }); await page.waitForTimeout(300);
await page.mouse.click(560, 380);
await page.waitForTimeout(1000);
console.log("after 1st:", await page.locator("footer").innerText(), "|", (await page.locator(".tcv_cad_status, .tcv_status").first().innerText().catch(()=>"")));
await page.mouse.move(690, 255); await page.mouse.move(700, 262, { steps: 5 }); await page.waitForTimeout(300);
await page.mouse.click(700, 262);

await page.waitForTimeout(1200);
await page.screenshot({ path: OUT + "measure-distance.png" });
console.log("distance:", (await page.locator(".tcv_distance_measurement_panel").innerText().catch(() => "n/a")).replace(/\s+/g, " "));
console.log(logs.filter((l) => !l.startsWith("debug")).slice(0, 20).join("\n"));
await browser.close();
