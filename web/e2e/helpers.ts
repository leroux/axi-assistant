import type { Page } from "@playwright/test";

/**
 * Inject a synthetic WebSocket message into the React app.
 * Requires the dev-mode `window.__testWs` hook in useWebSocket.ts.
 */
export async function injectWsMessage(
  page: Page,
  msg: Record<string, unknown>,
): Promise<void> {
  await page.evaluate((data) => {
    const ws = (window as unknown as Record<string, WebSocket>).__testWs;
    if (!ws) throw new Error("__testWs not available");
    // Directly invoke onmessage handler for reliable injection
    if (ws.onmessage) {
      ws.onmessage(new MessageEvent("message", { data: JSON.stringify(data) }));
    }
  }, msg);
}

/**
 * Wait for a chat message with the given role to appear.
 * Returns the locator for the last matching message.
 */
export function lastMessage(page: Page, role: string) {
  return page.getByTestId(`msg-${role}`).last();
}

/**
 * Wait for the WebSocket to show "Connected" status.
 */
export async function waitForConnected(page: Page): Promise<void> {
  await page.getByTestId("connection-status").filter({ hasText: "Connected" }).waitFor();
}

/**
 * Enable system messages display (toggle defaults to hidden).
 */
export async function enableSystemMessages(page: Page): Promise<void> {
  const toggle = page.getByTestId("toggle-system");
  if (!(await toggle.evaluate((el) => el.classList.contains("active")))) {
    await toggle.click();
  }
}

/**
 * Send a message via the UI and wait for it to appear.
 */
export async function sendMessage(page: Page, text: string): Promise<void> {
  const input = page.getByRole("textbox", { name: /type a message/i });
  await input.fill(text);
  await page.getByRole("button", { name: "Send" }).click();
}
