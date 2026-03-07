import { test, expect } from "@playwright/test";
import { waitForConnected } from "./helpers";

test.describe("WebSocket reconnection", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await waitForConnected(page);
  });

  test("closing WebSocket shows Disconnected status", async ({ page }) => {
    // Force-close the WebSocket
    await page.evaluate(() => {
      const ws = (window as unknown as Record<string, WebSocket>).__testWs;
      if (ws) ws.close();
    });
    await expect(page.getByTestId("connection-status")).toHaveText("Disconnected");
  });

  test("auto-reconnects after disconnect", async ({ page }) => {
    // Close and wait for reconnect (2s timer + connection time)
    await page.evaluate(() => {
      const ws = (window as unknown as Record<string, WebSocket>).__testWs;
      if (ws) ws.close();
    });
    await expect(page.getByTestId("connection-status")).toHaveText("Disconnected");
    // Should auto-reconnect within ~5 seconds
    await expect(page.getByTestId("connection-status")).toHaveText("Connected", {
      timeout: 10_000,
    });
  });

  test("agent list restored after reconnect", async ({ page }) => {
    // Verify agents are present
    await expect(page.getByTestId("agent-item-axi-master")).toBeVisible();

    // Disconnect
    await page.evaluate(() => {
      const ws = (window as unknown as Record<string, WebSocket>).__testWs;
      if (ws) ws.close();
    });

    // Wait for reconnect
    await expect(page.getByTestId("connection-status")).toHaveText("Connected", {
      timeout: 10_000,
    });

    // Agents should be back
    await expect(page.getByTestId("agent-item-axi-master")).toBeVisible();
  });
});
