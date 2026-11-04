// evidence.ts - copy next to your generated specs (e.g. tests/evidence.ts).
// Every user action goes through actionEvidence(); every pure check through observationEvidence().
// Both run the assertion first and take a full-page screenshot only after it passed.
import { errors, type Page, type TestInfo } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const ROOT = process.env.EVIDENCE_DIR ?? "evidence";

const safe = (s: string, max = 100) => s.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^-|-$/g, "").slice(0, max) || "x";

/** Folder this test's screenshots go into: <EVIDENCE_DIR>/<test title>/ */
export function evidenceDir(testInfo: TestInfo): string {
  const dir = path.join(ROOT, safe(testInfo.titlePath.slice(1).join("--") || testInfo.title));
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

export async function actionEvidence(
  page: Page,
  label: string,
  action: () => Promise<unknown>,
  verify: () => Promise<unknown>,
  dir: string,
): Promise<string> {
  try {
    await action();
  } catch (e) {
    // One retry for "found but not interactable yet" (a transition or re-render), nothing more.
    if (!(e instanceof errors.TimeoutError)) throw e;
    await page.waitForTimeout(500);
    await action();
  }
  await verify(); // the assertion lives INSIDE verify; a screenshot of a failed check is never produced
  await page.waitForLoadState("networkidle", { timeout: 3000 }).catch(() => {});
  const file = path.join(dir, `${safe(label)}.png`);
  await page.screenshot({ path: file, fullPage: true });
  return file;
}

export async function observationEvidence(
  page: Page,
  label: string,
  verify: () => Promise<unknown>,
  dir: string,
): Promise<string> {
  return actionEvidence(page, label, async () => {}, verify, dir);
}
