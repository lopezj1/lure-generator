// Section screenshots for the #57 report.
import { chromium } from "playwright";
const shots = new URL("../shots/", import.meta.url).pathname;
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 900, height: 900 } });
await p.goto("http://localhost:5757/");
await p.waitForSelector("form");
const sect = (key) => p.locator(`[data-layout="object-field"]:has(> [data-layout="object-field-meta"] #root_${key}__title)`).first();
for (const k of ["size", "joint", "buoyancy", "ballast", "process"]) {
  await sect(k).screenshot({ path: `${shots}${k}.png` });
}
const hw = p.locator('[data-layout="array-field"]:has(> [data-layout="array-field-meta"] #root_hardware__title)');
await hw.locator('[data-layout="array-item"]').first().screenshot({ path: `${shots}hardware-item.png` });
// Server error inline: set both size fields, submit.
await p.locator("#root_size_target_weight_oz").fill("2");
await p.click("button[type=submit]");
await p.waitForTimeout(800);
await sect("size").screenshot({ path: `${shots}size-server-error.png` });
// Client error inline: out-of-range joint angle via number box.
await p.locator("#root_joint_angle_deg").fill("60");
await p.locator("#root_joint_angle_deg").blur();
await p.click("button[type=submit]");
await p.waitForTimeout(800);
await sect("joint").screenshot({ path: `${shots}joint-client-error.png` });
await b.close();
