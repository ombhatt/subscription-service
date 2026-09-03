import { FakeApi, expect, mockApi, signInAs, test } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await signInAs(page, "bob");
});

test("a locked model names the tier that unlocks it", async ({ page, api }) => {
  await page.goto("/chat");

  await page.locator("select").selectOption("reasoning");
  await page.getByPlaceholder("Say something…").fill("does the paywall work?");
  await page.getByRole("button", { name: "Send" }).click();

  const banner = page.locator(".banner.warn");
  await expect(banner).toContainText("reasoning");
  await expect(banner).toContainText("not on the free plan");
  await expect(banner).toContainText("needs pro");
  await expect(banner.getByRole("link", { name: "See plans" })).toBeVisible();
});

test("a blocked model consumes no quota", async ({ page, api }) => {
  // The feature check runs before metering; being refused must not cost you a
  // message.
  await page.goto("/chat");
  await expect(page.getByText("20 of 20 messages left today")).toBeVisible();

  await page.locator("select").selectOption("large");
  await page.getByPlaceholder("Say something…").fill("hello");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.locator(".banner.warn")).toBeVisible();
  await expect(page.getByText("20 of 20 messages left today")).toBeVisible();
  expect(api.state.messagesUsed).toBe(0);
});

test("an allowed model answers and decrements the quota", async ({ page }) => {
  await page.goto("/chat");

  await page.getByPlaceholder("Say something…").fill("hello from the free tier");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.locator(".bubble.them")).toContainText("[free] echo: hello from the free tier");
  await expect(page.getByText("19 of 20 messages left today")).toBeVisible();
});

test("running out of messages says when it resets and what lifts it", async ({ page }) => {
  const api = new FakeApi({ messagesUsed: 20 });
  await mockApi(page, api);
  await page.goto("/chat");

  await page.getByPlaceholder("Say something…").fill("one too many");
  await page.getByRole("button", { name: "Send" }).click();

  const banner = page.locator(".banner.warn");
  await expect(banner).toContainText("Daily limit reached");
  await expect(banner).toContainText("all 20 messages");
  await expect(banner).toContainText("Resets");
  await expect(banner).toContainText("move to plus");
  await expect(banner.getByRole("link", { name: "Upgrade" })).toBeVisible();
});

test("a Pro user is offered no upgrade when nothing is above them", async ({ page }) => {
  const api = new FakeApi({
    tier: "pro",
    status: "active",
    source: "subscription",
    messagesUsed: 1500,
  });
  await mockApi(page, api);
  await page.goto("/chat");

  await page.getByPlaceholder("Say something…").fill("over the top");
  await page.getByRole("button", { name: "Send" }).click();

  const banner = page.locator(".banner.warn");
  await expect(banner).toContainText("Daily limit reached");
  await expect(banner.getByRole("link", { name: "Upgrade" })).toHaveCount(0);
});

test("locked models are visible but labelled", async ({ page }) => {
  // A paywall you cannot see is not a paywall.
  await page.goto("/chat");
  const options = page.locator("select option");
  await expect(options).toHaveCount(3);
  await expect(options.nth(0)).toHaveText("small");
  await expect(options.nth(1)).toHaveText("large (locked)");
  await expect(options.nth(2)).toHaveText("reasoning (locked)");
});
