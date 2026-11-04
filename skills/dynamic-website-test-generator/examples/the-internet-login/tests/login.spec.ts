import { test, expect } from "@playwright/test";
import { actionEvidence, observationEvidence, evidenceDir } from "./evidence";

const URL = "https://the-internet.herokuapp.com/login";
// The demo page prints its own test credentials ("Enter tomsmith ... SuperSecretPassword!"); these are not real ones.
const DEMO_USER = "tomsmith";
const DEMO_PASS = "SuperSecretPassword!";

test("login page shows its heading, username and password fields and the login button", async ({ page }, testInfo) => {
  const dir = evidenceDir(testInfo);
  await page.goto(URL);
  await observationEvidence(page, "login-form-present", async () => {
    await expect(page.getByRole("heading", { name: "Login Page", exact: true })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Username", exact: true })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Password", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "\uf090 Login", exact: true })).toBeVisible();
  }, dir);
});

test("logging in with the credentials printed on the page leaves the login page for /secure", async ({ page }, testInfo) => {
  const dir = evidenceDir(testInfo);
  await page.goto(URL);
  await page.getByRole("textbox", { name: "Username", exact: true }).fill(DEMO_USER);   // setup
  await page.getByRole("textbox", { name: "Password", exact: true }).fill(DEMO_PASS);   // setup
  await actionEvidence(page, "login-submitted", async () => {
    await page.getByRole("button", { name: "\uf090 Login", exact: true }).click();
  }, async () => {
    await expect(page).toHaveURL(/\/secure/);
  }, dir);
});

test("submitting a wrong password keeps the visitor on the login page", async ({ page }, testInfo) => {
  const dir = evidenceDir(testInfo);
  await page.goto(URL);
  await page.getByRole("textbox", { name: "Username", exact: true }).fill(DEMO_USER);   // setup
  await page.getByRole("textbox", { name: "Password", exact: true }).fill("not-the-password");   // setup
  await actionEvidence(page, "wrong-password-submitted", async () => {
    await page.getByRole("button", { name: "\uf090 Login", exact: true }).click();
  }, async () => {
    await expect(page).not.toHaveURL(/\/secure/);
    await expect(page.getByRole("heading", { name: "Login Page", exact: true })).toBeVisible();
  }, dir);
});
