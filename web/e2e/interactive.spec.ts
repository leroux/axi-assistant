import { test, expect } from "@playwright/test";
import { enableSystemMessages, injectWsMessage, waitForConnected } from "./helpers";

test.describe("interactive features (WS injection)", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await waitForConnected(page);
    await enableSystemMessages(page);
  });

  test.describe("plan approval", () => {
    test("plan request shows approval panel", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "plan_request",
        agent,
        plan: "Step 1: Do the thing\nStep 2: Profit",
        ts: Date.now() / 1000,
      });
      await expect(page.getByTestId("plan-approval")).toBeVisible();
      await expect(page.getByTestId("plan-content")).toContainText("Step 1: Do the thing");
    });

    test("approve dismisses panel and shows system message", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "plan_request",
        agent,
        plan: "Test plan",
        ts: Date.now() / 1000,
      });
      await expect(page.getByTestId("plan-approval")).toBeVisible();
      await page.getByRole("button", { name: "Approve" }).click();
      await expect(page.getByTestId("plan-approval")).not.toBeVisible();
      await expect(
        page.getByTestId("msg-system").filter({ hasText: "Plan approved" }),
      ).toBeVisible();
    });

    test("reject with feedback shows feedback in system message", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "plan_request",
        agent,
        plan: "Bad plan",
        ts: Date.now() / 1000,
      });
      await page.getByPlaceholder("Optional feedback...").fill("Needs more detail");
      await page.getByRole("button", { name: "Reject" }).click();
      await expect(page.getByTestId("plan-approval")).not.toBeVisible();
      await expect(
        page.getByTestId("msg-system").filter({ hasText: "Needs more detail" }),
      ).toBeVisible();
    });
  });

  test.describe("question form", () => {
    test("question message shows form with options", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "question",
        agent,
        questions: [
          {
            question: "Which color?",
            header: "Color",
            options: [
              { label: "Red", description: "A warm color" },
              { label: "Blue", description: "A cool color" },
            ],
            multiSelect: false,
          },
        ],
        ts: Date.now() / 1000,
      });
      await expect(page.getByTestId("question-form")).toBeVisible();
      await expect(page.getByTestId("question-block-0")).toBeVisible();
      await expect(page.getByText("Which color?")).toBeVisible();
      await expect(page.getByText("Red")).toBeVisible();
      await expect(page.getByText("Blue")).toBeVisible();
    });

    test("submit answers dismisses form", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "question",
        agent,
        questions: [
          {
            question: "Pick one",
            options: [
              { label: "Option A" },
              { label: "Option B" },
            ],
            multiSelect: false,
          },
        ],
        ts: Date.now() / 1000,
      });
      // Submit button should be disabled until an option is selected
      await expect(page.getByRole("button", { name: "Submit Answers" })).toBeDisabled();
      // Select an option
      await page.getByText("Option A").click();
      await expect(page.getByRole("button", { name: "Submit Answers" })).toBeEnabled();
      await page.getByRole("button", { name: "Submit Answers" }).click();
      await expect(page.getByTestId("question-form")).not.toBeVisible();
      await expect(
        page.getByTestId("msg-system").filter({ hasText: "Answers submitted" }),
      ).toBeVisible();
    });
  });

  test.describe("todo list", () => {
    test("todo update shows tasks in sidebar", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "todo_update",
        agent,
        todos: [
          { content: "Write tests", status: "completed", activeForm: "Writing tests" },
          { content: "Fix bugs", status: "in_progress", activeForm: "Fixing bugs" },
          { content: "Deploy", status: "pending" },
        ],
        ts: Date.now() / 1000,
      });
      await expect(page.getByTestId("todo-list")).toBeVisible();
      await expect(page.getByTestId("todo-item-0")).toContainText("Write tests");
      await expect(page.getByTestId("todo-item-1")).toContainText("Fixing bugs");
      await expect(page.getByTestId("todo-item-2")).toContainText("Deploy");
    });
  });

  test.describe("streaming", () => {
    test("TextDelta accumulates streaming text", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "stream_event",
        event_type: "TextDelta",
        agent,
        data: { text: "Hello " },
        ts: Date.now() / 1000,
      });
      await injectWsMessage(page, {
        type: "stream_event",
        event_type: "TextDelta",
        agent,
        data: { text: "world" },
        ts: Date.now() / 1000,
      });
      // Use .msg-text to avoid matching the "Axi" role label
      // Allow debounced streaming to settle (80ms debounce + React render)
      await page.waitForTimeout(500);
      const streamText = page.getByTestId("streaming-indicator").locator(".msg-text");
      await expect(streamText).toContainText("Hello world", { timeout: 5_000 });
    });

    test("TextFlush clears streaming and shows final message", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      // Start streaming
      await injectWsMessage(page, {
        type: "stream_event",
        event_type: "TextDelta",
        agent,
        data: { text: "partial" },
        ts: Date.now() / 1000,
      });
      await expect(page.getByTestId("streaming-indicator")).toBeVisible();

      // Flush
      await injectWsMessage(page, {
        type: "stream_event",
        event_type: "TextFlush",
        agent,
        data: { text: "Final response text", reason: "end_turn" },
        ts: Date.now() / 1000,
      });
      // Streaming indicator should be gone (allow debounce + render)
      await page.waitForTimeout(200);
      await expect(page.getByTestId("streaming-indicator")).not.toBeVisible();
      // Final assistant message should appear
      await expect(
        page.getByTestId("msg-assistant").filter({ hasText: "Final response text" }),
      ).toBeVisible();
    });

    test("StreamEnd shows completion timing", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "stream_event",
        event_type: "StreamEnd",
        agent,
        data: { elapsed_s: 2.5, msg_count: 1, flush_count: 1 },
        ts: Date.now() / 1000,
      });
      await expect(
        page.getByTestId("msg-system").filter({ hasText: "Completed in 2.5s" }),
      ).toBeVisible();
    });

    test("markdown renders in assistant messages", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "stream_event",
        event_type: "TextFlush",
        agent,
        data: { text: "This is **bold** and *italic* and `code`", reason: "end_turn" },
        ts: Date.now() / 1000,
      });
      const msg = page.getByTestId("msg-assistant").filter({ hasText: "bold" });
      await expect(msg).toBeVisible();
      const html = await msg.locator(".msg-text").innerHTML();
      expect(html).toContain("<strong>");
      expect(html).toContain("<em>");
      expect(html).toContain("<code>");
      expect(html).not.toContain("**bold**");
    });

    test("ThinkingStart shows thinking message", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "stream_event",
        event_type: "ThinkingStart",
        agent,
        data: {},
        ts: Date.now() / 1000,
      });
      await expect(
        page.getByTestId("msg-system").filter({ hasText: "Thinking..." }).last(),
      ).toBeVisible();
    });
  });

  test.describe("lifecycle events", () => {
    test("agent_wake adds system message", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "agent_wake",
        agent,
        ts: Date.now() / 1000,
      });
      await expect(
        page.getByTestId("msg-system").filter({ hasText: /woke up/ }).last(),
      ).toBeVisible();
    });

    test("agent_sleep adds system message", async ({ page }) => {
      const agent = await page.getByTestId("active-agent-name").textContent();
      await injectWsMessage(page, {
        type: "agent_sleep",
        agent,
        ts: Date.now() / 1000,
      });
      await expect(
        page.getByTestId("msg-system").filter({ hasText: /went to sleep/ }).last(),
      ).toBeVisible();
    });

    test("agent_spawn adds new agent to list", async ({ page }) => {
      await injectWsMessage(page, {
        type: "agent_spawn",
        agent: "new-test-agent",
        ts: Date.now() / 1000,
      });
      // Short timeout: periodic agent refresh (10s) overwrites injected agents
      await expect(page.getByTestId("agent-item-new-test-agent")).toBeVisible({ timeout: 3_000 });
    });

    test("agent_kill removes agent from list", async ({ page }) => {
      // First spawn, then kill
      await injectWsMessage(page, {
        type: "agent_spawn",
        agent: "temp-agent",
        ts: Date.now() / 1000,
      });
      await expect(page.getByTestId("agent-item-temp-agent")).toBeVisible();

      await injectWsMessage(page, {
        type: "agent_kill",
        agent: "temp-agent",
        ts: Date.now() / 1000,
      });
      await expect(page.getByTestId("agent-item-temp-agent")).not.toBeVisible();
    });
  });
});
