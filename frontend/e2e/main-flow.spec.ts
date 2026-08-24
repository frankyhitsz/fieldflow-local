import { expect, test } from '@playwright/test'

test('baseline, optimize, compare, arbitrary activation, and version report', async ({ page, request }) => {
  const created = await request.post('/api/scenarios', { data: { fixture_id: 'main', name: 'E2E 主流程场景' } })
  expect(created.ok()).toBeTruthy()
  const scenarioId = (await created.json()).id as string

  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'FieldFlow' })).toBeHidden()
  await expect(page.getByRole('heading', { name: '今日调度 · 城西片区' })).toBeVisible()
  await page.getByLabel('业务场景').selectOption(scenarioId)
  await expect(page.getByRole('heading', { name: 'E2E 主流程场景' })).toBeVisible()

  await page.getByRole('button', { name: '生成基线' }).click()
  await expect(page.getByText(/人工基线 · V001/)).toBeVisible({ timeout: 15_000 })
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
  await v1.getByRole('button', { name: '重新激活' }).click()
  await expect(page.locator('.command-context').getByText(/V003/)).toBeVisible({ timeout: 15_000 })

  const versions = await request.get(`/api/scenarios/${scenarioId}/plan-versions`)
  expect(versions.ok()).toBeTruthy()
  const plans = await versions.json()
  expect(plans.map((item: { number: number }) => item.number)).toEqual([1, 2, 3])
  const report = await request.get(`/api/scenarios/${scenarioId}/plan-versions/${plans[0].id}/report`)
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

test('dispatch workspace keeps its primary regions visible at 1440 by 900', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '工单队列' })).toBeVisible()
  await expect(page.getByRole('img', { name: '工单位置与技师路线图' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '技师时间轴' })).toBeVisible()
  const regions = await page.locator('.queue, .map-panel, .timeline').evaluateAll(items => items.map(item => {
    const box = item.getBoundingClientRect()
    return { left: box.left, top: box.top, right: box.right, bottom: box.bottom }
  }))
  for (const box of regions) {
    expect(box.left).toBeGreaterThanOrEqual(0)
    expect(box.top).toBeGreaterThanOrEqual(0)
    expect(box.right).toBeLessThanOrEqual(1440)
    expect(box.bottom).toBeLessThanOrEqual(900)
  }
})

test('changing a lock marks the displayed plan stale', async ({ page, request }) => {
  const created = await request.post('/api/scenarios', { data: { fixture_id: 'main', name: 'E2E 过期门禁场景' } })
  expect(created.ok()).toBeTruthy()
  const scenarioId = (await created.json()).id as string
  const baseline = await request.post(`/api/scenarios/${scenarioId}/baseline`)
  expect(baseline.ok()).toBeTruthy()
  const versions = await request.get(`/api/scenarios/${scenarioId}/plan-versions`)
  const plans = await versions.json()
  const active = plans.find((item: { active: boolean }) => item.active)
  expect(active).toBeDefined()
  const assignment = active!.selected.assignments[0]

  await page.goto('/')
  await page.getByLabel('业务场景').selectOption(scenarioId)
  await expect(page.getByRole('heading', { name: 'E2E 过期门禁场景' })).toBeVisible()
  await page.getByRole('button', { name: '全部' }).click()
  await page.getByText(assignment.work_order_id, { exact: false }).first().click()
  await page.getByRole('button', { name: '锁定此工单与技师' }).click()
  await expect(page.getByText('业务数据已修改，现有方案不再适用')).toBeVisible()
  await expect(page.getByText(/D\d{3}/).first()).toBeVisible()
})
