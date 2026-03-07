import { test, expect } from "@playwright/test";
import { enableSystemMessages, lastMessage, sendMessage, waitForConnected } from "./helpers";

test.describe("messaging", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await waitForConnected(page);
  });

  test("send button disabled when input empty", async ({ page }) => {
    await expect(page.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  test("send button disabled when input is whitespace", async ({ page }) => {
    await page.getByRole("textbox", { name: /type a message/i }).fill("   ");
    await expect(page.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  test("send button enabled when input has text", async ({ page }) => {
    await page.getByRole("textbox", { name: /type a message/i }).fill("hello");
    await expect(page.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  test("sending message shows user message in chat", async ({ page }) => {
    await sendMessage(page, "test message from playwright");
    const msg = lastMessage(page, "user");
    await expect(msg).toBeVisible();
    await expect(msg).toContainText("test message from playwright");
  });

  test("input clears after sending", async ({ page }) => {
    await sendMessage(page, "clear test");
    await expect(page.getByRole("textbox", { name: /type a message/i })).toHaveValue("");
  });

  test("bot responds with assistant message", async ({ page }) => {
    await sendMessage(page, "Say exactly: pong");
    // Wait for an assistant message to appear
    const msg = lastMessage(page, "assistant");
    await expect(msg).toBeVisible({ timeout: 30_000 });
    // Verify it has non-empty text (catches missing TextFlush bug)
    const text = await msg.textContent();
    expect(text?.trim().length).toBeGreaterThan(0);
  });

  test("system messages appear during response", async ({ page }) => {
    await enableSystemMessages(page);
    await sendMessage(page, "Hi");
    // "woke up" or "Thinking..." should appear as system messages
    const systemMsgs = page.getByTestId("msg-system");
    await expect(systemMsgs.first()).toBeVisible({ timeout: 30_000 });
  });

  test("completed timing appears after response", async ({ page }) => {
    await enableSystemMessages(page);
    await sendMessage(page, "Say ok");
    // Wait for "Completed in" system message
    await expect(
      page.getByTestId("msg-system").filter({ hasText: /Completed in/ }).last(),
    ).toBeVisible({ timeout: 30_000 });
  });

  test("messages persist when switching agents and back", async ({ page }) => {
    await sendMessage(page, "persist test message");
    await expect(lastMessage(page, "user")).toContainText("persist test message");

    // Switch to another agent
    const agents = page.getByTestId("agent-list").locator("[data-testid^='agent-item-']");
    const count = await agents.count();
    if (count < 2) {
      test.skip();
      return;
    }
    await agents.nth(1).click();
    // Chat area should not contain our message
    await expect(page.getByTestId("chat-area")).not.toContainText("persist test message");

    // Switch back
    await agents.nth(0).click();
    // Message should still be there
    await expect(page.getByTestId("chat-area")).toContainText("persist test message");
  });

  // Markdown rendering is tested deterministically in interactive.spec.ts via WS injection
});
