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
    board: (() => {
      const element = document.querySelector('.studio-board-workspace');
      return element ? element.scrollWidth - element.clientWidth : -1;
    })(),
  }));
  expect(overflow.document).toBeLessThanOrEqual(0);
  expect(overflow.board).toBeLessThanOrEqual(0);
}

test('Agent 模块总览与独立交互页适配桌面和移动端', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.addInitScript(() => localStorage.setItem('codepilot.theme', 'light'));
  await login(page);

  await expect(page.getByRole('region', { name: 'Agent 总览' })).toBeVisible();
  const agentCount = await page.locator('.agent-module').count();
  expect(agentCount).toBeGreaterThanOrEqual(2);
  await expect(page.locator('.task-lane')).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: 'output/playwright/agent-studio-redesign-desktop.png', fullPage: true, animations: 'disabled' });

  await page.getByRole('button', { name: '打开 build' }).click();
  await expect(page.locator('.studio-agent-workspace')).toBeVisible();
  await expect(page.locator('.studio-detail-drawer')).toHaveCount(0);
  await expect(page.locator('.studio-composer textarea')).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: 'output/playwright/agent-studio-redesign-detail.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '返回 Agent 总览' }).click();

  await page.setViewportSize({ width: 900, height: 900 });
  await page.waitForTimeout(300);
  await expect(page.locator('.agent-module')).toHaveCount(agentCount);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: 'output/playwright/agent-studio-redesign-900.png', fullPage: true, animations: 'disabled' });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  await expect(page.locator('.agent-module')).toHaveCount(agentCount);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: 'output/playwright/agent-studio-redesign-mobile.png', fullPage: true, animations: 'disabled' });

  await page.getByRole('button', { name: '切换为暗色模式' }).first().click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.getByRole('button', { name: '打开 Agent 导航' }).click();
  for (const name of ['公共 Agent', '技能', '执行钩子']) {
    await page.getByRole('link', { name }).click();
    await expect(page.locator('.studio-resource-workspace')).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.getByRole('button', { name: '打开 Agent 导航' }).click();
  }
  await page.screenshot({ path: 'output/playwright/agent-studio-redesign-mobile-dark.png', fullPage: true, animations: 'disabled' });
});
