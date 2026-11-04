import { test, expect } from "@playwright/test";
import { actionEvidence, observationEvidence, evidenceDir } from "./evidence";

const URL = "https://demo.playwright.dev/todomvc";

test("todos heading and new-todo textbox are present on load", async ({ page }, testInfo) => {
  const dir = evidenceDir(testInfo);
  await page.goto(URL);
  const heading = page.getByRole("heading", { name: "todos", exact: true });
  const input = page.getByRole("textbox", { name: "What needs to be done?", exact: true });
  await observationEvidence(page, "heading-and-input-visible", async () => {
    await expect(heading).toBeVisible();
    await expect(input).toBeVisible();
  }, dir);
});

test("adding a todo with Enter lists it and updates the items-left counter", async ({ page }, testInfo) => {
  const dir = evidenceDir(testInfo);
  await page.goto(URL);
  const input = page.getByRole("textbox", { name: "What needs to be done?", exact: true });
  await actionEvidence(page, "todo-added", async () => {
    await input.fill("buy milk");
    await input.press("Enter");
  }, async () => {
    await expect(page.getByTestId("todo-title")).toHaveText(["buy milk"]);
    await expect(page.getByTestId("todo-count")).toContainText("1");
  }, dir);
});

test("ticking a todo's checkbox marks it completed", async ({ page }, testInfo) => {
  const dir = evidenceDir(testInfo);
  await page.goto(URL);
  const input = page.getByRole("textbox", { name: "What needs to be done?", exact: true });
  const item = page.getByTestId("todo-item");
  await actionEvidence(page, "todo-added-for-setup", async () => {
    await input.fill("water plants");
    await input.press("Enter");
  }, async () => {
    await expect(item).toHaveCount(1);
  }, dir);
  await actionEvidence(page, "todo-completed", async () => {
    await item.getByRole("checkbox").check();
  }, async () => {
    await expect(item).toHaveClass(/completed/);
  }, dir);
});
