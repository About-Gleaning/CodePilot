import { expect, test, type Page } from '@playwright/test';

import { readE2eState } from './global-setup';

async function login(page: Page) {
  const state = readE2eState();
  await page.goto('/');
  await page.getByLabel('用户名').fill('alice');
  await page.getByLabel('密码').fill(state.password);
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page.locator('.studio-user-menu')).toContainText('alice');
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    console: (() => {
      const element = document.querySelector('.config-console');
      return element ? element.scrollWidth - element.clientWidth : -1;
    })(),
  }));
  expect(overflow.document).toBeLessThanOrEqual(0);
  expect(overflow.console).toBeLessThanOrEqual(0);
}

test('Agent 配置控制台适配桌面、临界宽度和移动端', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.addInitScript(() => localStorage.setItem('codepilot.theme', 'light'));
  await login(page);
  await page.getByRole('button', { name: '新建 Agent' }).click();

  const rail = page.getByRole('complementary', { name: 'Agent 配置分区' });
  await expect(rail).toBeVisible();
  await expect(rail.getByRole('button')).toHaveCount(6);
  await expect(page.getByRole('heading', { name: '定义这个 Agent' })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: 'output/playwright/agent-config-redesign-desktop.png', fullPage: true, animations: 'disabled' });

  await page.setViewportSize({ width: 900, height: 900 });
  await page.waitForTimeout(450);
  await expect(rail).toBeHidden();
  const sectionSelector = page.locator('select').filter({ has: page.locator('option[value="overview"]') });
  await expect(sectionSelector).toBeVisible();
  await sectionSelector.selectOption('capabilities');
  await expect(page.getByRole('heading', { name: '能力边界' })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: 'output/playwright/agent-config-redesign-900.png', fullPage: true, animations: 'disabled' });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(450);
  await sectionSelector.selectOption('connections');
  await expect(page.getByRole('heading', { name: '连接身份' })).toBeVisible();
  await expect(page.getByRole('button', { name: '保存 Agent' })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: 'output/playwright/agent-config-redesign-mobile-light.png', fullPage: true, animations: 'disabled' });

  await page.getByRole('button', { name: '切换为暗色模式' }).first().click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: 'output/playwright/agent-config-redesign-mobile-dark.png', fullPage: true, animations: 'disabled' });
});
