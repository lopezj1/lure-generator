// Spike #57: drive the form headlessly, screenshot, and exercise the evaluation scenarios.
import { chromium } from "playwright";
import { writeFileSync } from "node:fs";

const shots = new URL("../shots/", import.meta.url).pathname;
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1000, height: 1400 } });
const logs = [];
page.on("console", (m) => logs.push(`${m.type()}: ${m.text()}`));
page.on("pageerror", (e) => logs.push(`pageerror: ${e.message}`));
await page.goto("http://localhost:5757/");
await page.waitForSelector("form");
await page.waitForTimeout(500);
await page.screenshot({ path: shots + "01-full.png", fullPage: true });

const report = {};
const snap = () => page.evaluate(() => JSON.parse(JSON.stringify(window.__snapshot())));
const submit = async () => {
  await page.click("button[type=submit]");
  await page.waitForFunction(() => document.querySelector("#status")?.textContent);
  await page.waitForTimeout(400);
  return page.textContent("#status");
};

// 1. Submit untouched -> server round-trip.
report.untouched = await submit();
writeFileSync(shots + "out-untouched.json", JSON.stringify(await page.evaluate(() => window.__formOutput), null, 1));

// 2. Slider + number box share a value.
const gap = page.locator('input[type=range][aria-label^="Gap"]');
await gap.fill("3");
report.gapAfterSlider = (await snap()).joint.gap_mm;

// 3. Range pair edit.
await page.locator('input[aria-label="Head density max"]').fill("1.1");
report.headDensity = (await snap()).buoyancy.head_density;

// 4. Material select.
await page.locator("select").filter({ hasText: "tungsten_putty" }).selectOption("lead");
report.ballastMaterial = (await snap()).ballast.material;

// 5. Hardware add / move / remove (scoped: dfm.cnc.tools is an array too).
const hw = page.locator('[data-layout="array-field"]:has(> [data-layout="array-field-meta"] #root_hardware__title)');
const before = (await snap()).hardware.length;
await hw.locator('button[data-type="array-item-move-down"]').first().click();
const afterMove = (await snap()).hardware.map((h) => h.name).slice(0, 2);
await hw.locator('button[data-type="array-item-remove"]').last().click();
const afterRemove = (await snap()).hardware.length;
await hw.locator('button[data-type="array-item-add"]').first().click();
const afterAdd = (await snap()).hardware;
report.hardware = { before, afterMove, afterRemove, afterAddLen: afterAdd.length, added: afterAdd.at(-1) };

// 6. Client-side (Ajv) error: added hardware item lacks id/x/z; joint angle out of range.
report.clientErrors = await submit();
report.clientErrorList = await page.evaluate(() => window.__clientErrors?.slice(0, 6));
await page.screenshot({ path: shots + "02-client-errors.png", fullPage: true });

// 7. Server-only error: exactly-one-of size. Remove the new item, set both size fields.
await hw.locator('button[data-type="array-item-remove"]').last().click();
await page.locator('input[id$="target_weight_oz"]').fill("2");
report.serverErrors = await submit();
report.serverResult = await page.evaluate(() => window.__serverResult);
const size = page.locator('input[id$="target_weight_oz"]').locator("xpath=ancestor::fieldset[1]");
await size.screenshot({ path: shots + "03-server-error-size.png" }).catch(() => {});
await page.screenshot({ path: shots + "03-server-error-full.png", fullPage: true });

// 8. Fix it: clear length -> should pass server.
await page.locator('input[id$="length_mm"][type=number]').fill("");
report.fixed = await submit();
writeFileSync(shots + "out-edited.json", JSON.stringify(await page.evaluate(() => window.__formOutput), null, 1));

report.logs = logs.filter((l) => !l.startsWith("debug"));
console.log(JSON.stringify(report, null, 1));
await browser.close();
