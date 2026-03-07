import { test, expect } from "@playwright/test";
import { waitForConnected } from "./helpers";

test.describe("basic connection & layout", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await waitForConnected(page);
  });

  test("page loads and shows Connected status", async ({ page }) => {
    await expect(page.getByTestId("connection-status")).toHaveText("Connected");
  });

  test("agent list shows at least axi-master", async ({ page }) => {
    await expect(page.getByTestId("agent-item-axi-master")).toBeVisible();
  });

  test("first agent is selected by default", async ({ page }) => {
    const agentName = await page.getByTestId("active-agent-name").textContent();
    expect(agentName).toBeTruthy();
    // The first agent in the list should match the status bar
    // Use .agent-name span to avoid including activity phase text
    const firstAgent = page.getByTestId("agent-list").locator("[data-testid^='agent-item-']").first();
    const firstName = await firstAgent.locator(".agent-name").textContent();
    expect(agentName).toBe(firstName);
  });

  test("clicking agent updates status bar", async ({ page }) => {
    // Find a second agent (if available)
    const agents = page.getByTestId("agent-list").locator("[data-testid^='agent-item-']");
    const count = await agents.count();
    if (count < 2) {
      test.skip();
      return;
    }
    const secondName = await agents.nth(1).locator(".agent-name").textContent();
    await agents.nth(1).click();
    await expect(page.getByTestId("active-agent-name")).toHaveText(secondName!);
  });

  test("chat area is visible", async ({ page }) => {
    await expect(page.getByTestId("chat-area")).toBeVisible();
  });

  test("message input is visible and enabled", async ({ page }) => {
    await expect(page.getByRole("textbox", { name: /type a message/i })).toBeEnabled();
  });
});
