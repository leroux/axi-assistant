import { test, expect } from "@playwright/test";
import { enableSystemMessages, sendMessage, waitForConnected } from "./helpers";

test.describe("agent lifecycle", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await waitForConnected(page);
    await enableSystemMessages(page);
  });

  test("sending message produces system messages", async ({ page }) => {
    await sendMessage(page, "lifecycle test");
    // At least one system message should appear (woke up, Thinking, Completed, etc.)
    await expect(
      page.getByTestId("msg-system").first(),
    ).toBeVisible({ timeout: 45_000 });
  });

  test("agent shows awake after message exchange", async ({ page }) => {
    await sendMessage(page, "awake test");
    // Wait for any assistant response or system message indicating completion
    await expect(
      page.getByTestId("msg-system").first(),
    ).toBeVisible({ timeout: 45_000 });

    const agentName = await page.getByTestId("active-agent-name").textContent();
    if (!agentName) return;
    const agentItem = page.getByTestId(`agent-item-${agentName}`);
    const dot = agentItem.locator(".status-dot");
    // Agent should be awake (or recently was) after receiving a message
    await expect(dot).toHaveClass(/awake/, { timeout: 10_000 });
  });
});
