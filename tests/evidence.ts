import type { Page, TestInfo } from '@playwright/test';

function fileSafe(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'step';
}

/** Perform one user action, verify its post-condition, and attach evidence of that state. */
export async function actionEvidence(
  page: Page,
  testInfo: TestInfo,
  label: string,
  action: () => Promise<void>,
  verify: () => Promise<void>,
) {
  await action();
  await verify();
  const path = testInfo.outputPath(`${fileSafe(label)}.png`);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(label, { path, contentType: 'image/png' });
}

export async function observationEvidence(
  page: Page,
  testInfo: TestInfo,
  label: string,
  verify: () => Promise<void>,
) {
  await verify();
  const path = testInfo.outputPath(`${fileSafe(label)}.png`);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(label, { path, contentType: 'image/png' });
}
