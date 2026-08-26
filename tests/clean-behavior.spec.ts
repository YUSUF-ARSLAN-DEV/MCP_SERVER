import { test, expect } from '@playwright/test';
import { actionEvidence, observationEvidence } from './evidence';

test.describe('https://sat.aljazeera.net/en', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('https://sat.aljazeera.net/en', { waitUntil: 'domcontentloaded' });
    const cookie = page.getByRole('button', { name: 'Allow all' });
    if (await cookie.isVisible()) await cookie.click();
  });

  test('navigates to the subscribe page', async ({ page }, testInfo) => {
    await actionEvidence(page, testInfo, 'subscribe-page-after-navigation',
      () => page.getByRole('link', { name: 'Subscribe for updates' }).click(),
      async () => {
        await expect(page).toHaveURL(/.*\/en\/subscribe/);
        await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
      });
  });

  test('switches to Arabic', async ({ page }, testInfo) => {
    await actionEvidence(page, testInfo, 'arabic-language-after-switch',
      () => page.getByRole('link', { name: 'العربية' }).click(),
      async () => {
        await expect(page).toHaveURL(/.*\/ar/);
        await expect(page.getByRole('heading', { name: 'ابحث عن ترددات الجزيرة', level: 1 })).toBeVisible();
      });
  });

  test('navigates from Go to Map to the English map page', async ({ page }, testInfo) => {
    await actionEvidence(page, testInfo, 'go-to-map-after-navigation',
      () => page.getByRole('button', { name: 'Go to Map' }).click(),
      async () => {
        await expect(page).toHaveURL(/.*\/en\/map/);
        await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
      });
  });

  test('navigates from Tune Your Receiver to the English tuning page', async ({ page }, testInfo) => {
    await actionEvidence(page, testInfo, 'tune-your-receiver-after-navigation',
      () => page.getByRole('link', { name: 'Tune Your Receiver' }).click(),
      async () => {
        await expect(page).toHaveURL(/.*\/en\/tunereceiver/);
        await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
      });
  });
});

test.describe('https://sat.aljazeera.net/en/subscribe', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('https://sat.aljazeera.net/en/subscribe', { waitUntil: 'domcontentloaded' });
    const cookie = page.getByRole('button', { name: 'Allow all' });
    if (await cookie.isVisible()) await cookie.click();
  });

  test('selects Country * as Egypt and Channel * as Al Jazeera English Channel HD', async ({ page }, testInfo) => {
    const country = page.getByRole('combobox', { name: 'Country *' });
    const channel = page.getByRole('combobox', { name: 'Channel *' });
    await actionEvidence(page, testInfo, 'country-selected-post-state',
      () => country.selectOption('Egypt'), () => expect(country.locator('option:checked')).toHaveText('Egypt'));
    await actionEvidence(page, testInfo, 'channel-selected-post-state',
      () => channel.selectOption('Al Jazeera English Channel HD'),
      () => expect(channel.locator('option:checked')).toHaveText('Al Jazeera English Channel HD'));
  });

  test('switches the subscription page language to Arabic', async ({ page }, testInfo) => {
    await actionEvidence(page, testInfo, 'subscription-arabic-after-switch',
      () => page.getByRole('link', { name: 'العربية' }).click(),
      async () => {
        await expect(page).toHaveURL(/.*\/ar\/subscribe/);
        await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
      });
  });
});

test.describe('https://sat.aljazeera.net/en/frequency-search', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('https://sat.aljazeera.net/en/frequency-search', { waitUntil: 'domcontentloaded' });
    const cookie = page.getByRole('button', { name: 'Allow all' });
    if (await cookie.isVisible()) await cookie.click();
  });

  test('shows the subscription call to action', async ({ page }, testInfo) => {
    await observationEvidence(page, testInfo, 'subscription-cta-visible', async () => {
      await expect(page.getByRole('button', { name: 'Subscribe Now' })).toBeVisible();
      await expect(page.locator('body')).toContainText('To get the latest frequencies from Al Jazeera, please subscribe to our mailing list');
    });
  });

  test('navigates from the interactive map link to the English map page', async ({ page }, testInfo) => {
    await actionEvidence(page, testInfo, 'interactive-map-link-after-navigation',
      () => page.getByRole('link', { name: 'Use our interactive map to find Al Jazeera' }).click(),
      async () => {
        await expect(page).toHaveURL(/.*\/en\/map/);
        await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
      });
  });
});
