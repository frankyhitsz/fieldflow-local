import { expect, test } from '@playwright/test'

test('baseline, optimize, compare, arbitrary restore, and version report', async ({ page, request }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'FieldFlow' })).toBeHidden()
  await expect(page.getByRole('heading', { name: '今日调度 · 城西片区' })).toBeVisible()

  await page.getByRole('button', { name: '生成基线' }).click()
  await expect(page.getByText(/人工基线 · V001/)).toBeVisible()
  await page.getByRole('button', { name: '生成推荐方案' }).click()
  await expect(page.getByText(/优化方案 · V002/)).toBeVisible({ timeout: 15_000 })

  await page.getByRole('button', { name: '方案版本' }).click()
  await expect(page.getByRole('heading', { name: '方案版本' })).toBeVisible()
  await page.getByLabel('比较起点').selectOption({ index: 1 })
  await page.getByLabel('比较终点').selectOption({ index: 2 })
  await page.getByRole('button', { name: '比较' }).click()
  await expect(page.getByRole('heading', { name: '方案对比' })).toBeVisible()
  await page.locator('.compare-modal .icon-btn').click()

  const v1 = page.getByRole('button', { name: '打开 V001', exact: true }).locator('..')
  page.once('dialog', dialog => dialog.accept())
  await v1.getByRole('button', { name: '恢复为新版本' }).click()
  await expect(page.locator('.command-context').getByText(/V003/)).toBeVisible({ timeout: 15_000 })

  const versions = await request.get('/api/scenarios/main/plan-versions')
  expect(versions.ok()).toBeTruthy()
  const plans = await versions.json()
  expect(plans.map((item: { number: number }) => item.number)).toEqual([1, 2, 3])
  const report = await request.get(`/api/scenarios/main/plan-versions/${plans[0].id}/report`)
  expect(report.ok()).toBeTruthy()
  expect(await report.text()).toContain('FieldFlow 调度台')
})

test('critical navigation remains reachable at 200 percent zoom', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '今日调度 · 城西片区' })).toBeVisible()
  await page.evaluate(() => { document.documentElement.style.zoom = '2' })
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()
  await page.getByRole('button', { name: '方案版本' }).click()
  await expect(page.getByRole('heading', { name: '方案版本' })).toBeVisible()
})

test('changing a lock marks the displayed plan stale', async ({ page, request }) => {
  const versions = await request.get('/api/scenarios/main/plan-versions')
  const plans = await versions.json()
  const active = plans.find((item: { active: boolean }) => item.active)
  const assignment = active.selected.assignments[0]

  await page.goto('/')
  await page.getByRole('button', { name: '全部' }).click()
  await page.getByText(assignment.work_order_id, { exact: false }).first().click()
  await page.getByRole('button', { name: '锁定此工单与技师' }).click()
  await expect(page.getByText('业务数据已修改，现有方案不再适用')).toBeVisible()
  await expect(page.getByText(/D\d{3}/).first()).toBeVisible()
})
