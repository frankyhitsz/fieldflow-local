import { expect, test } from '@playwright/test'

test('edit, optimize, experiment, publish, restore, compare, and report stay coherent', async ({ request }) => {
  test.setTimeout(60_000)
  const created = await request.post('/api/scenarios', {
    data: { fixture_id: 'main', name: 'E2E 生命周期场景' },
  })
  expect(created.ok()).toBeTruthy()
  const scenario = await created.json()
  const scenarioId = scenario.id as string

  const edited = await request.put(`/api/scenarios/${scenarioId}/work-orders/WO-1021`, {
    data: { note: '客户确认上午到场' },
  })
  expect(edited.ok()).toBeTruthy()
  expect((await edited.json()).revision).toBe(1)

  const optimized = await request.post(`/api/scenarios/${scenarioId}/optimize`, {
    headers: { 'Idempotency-Key': 'e2e-optimize-001' },
    data: { strategy: 'balanced', time_limit_seconds: 1 },
  })
  expect(optimized.ok()).toBeTruthy()
  expect((await optimized.json()).version).toBe(1)

  const experimentResponse = await request.post(`/api/scenarios/${scenarioId}/strategy-experiments`, {
    data: {
      dataset: 'current',
      profile_ids: ['balanced', 'low_travel'],
      time_limit_seconds: 1,
    },
  })
  expect(experimentResponse.status()).toBe(202)
  let experiment = await experimentResponse.json()
  for (let attempt = 0; attempt < 100 && ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(experiment.status); attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 200))
    const progress = await request.get(`/api/scenarios/${scenarioId}/strategy-experiments/${experiment.id}`)
    expect(progress.ok()).toBeTruthy()
    experiment = await progress.json()
  }
  expect(['COMPLETED', 'COMPLETED_WITH_ERRORS']).toContain(experiment.status)
  const candidate = experiment.candidates.find((item: { publishable: boolean }) => item.publishable)
  expect(candidate).toBeDefined()

  const published = await request.post(`/api/scenarios/${scenarioId}/strategy-experiments/${experiment.id}/publish`, {
    data: { candidate_id: candidate.id, expected_revision: 1 },
  })
  expect(published.ok()).toBeTruthy()
  expect((await published.json()).number).toBe(2)

  const changedAfterExperiment = await request.put(`/api/scenarios/${scenarioId}/work-orders/WO-1021`, {
    data: { note: '发布后客户再次改约' },
  })
  expect(changedAfterExperiment.ok()).toBeTruthy()
  expect((await changedAfterExperiment.json()).revision).toBe(2)

  let versions = await (await request.get(`/api/scenarios/${scenarioId}/plan-versions`)).json()
  expect(versions.map((item: { number: number }) => item.number)).toEqual([1, 2])
  const source = versions[0]
  const previewResponse = await request.get(`/api/scenarios/${scenarioId}/plan-versions/${source.id}/rollback-preview`)
  expect(previewResponse.ok()).toBeTruthy()
  const preview = await previewResponse.json()
  expect(preview.modified_work_orders).toContain('WO-1021')
  expect(preview.current_plan_number).toBe(2)

  const restored = await request.post(`/api/scenarios/${scenarioId}/plan-versions/${source.id}/restore`, {
    data: {
      expected_revision: 2,
      confirmation_token: preview.confirmation_token,
      reason: '撤销客户误操作',
      idempotency_key: 'e2e-rollback-001',
    },
  })
  expect(restored.ok()).toBeTruthy()
  expect((await restored.json()).number).toBe(3)

  versions = await (await request.get(`/api/scenarios/${scenarioId}/plan-versions`)).json()
  expect(versions.map((item: { number: number }) => item.number)).toEqual([1, 2, 3])
  const comparison = await request.get(
    `/api/scenarios/${scenarioId}/comparison?before=${versions[0].id}&after=${versions[2].id}`,
  )
  expect(comparison.ok()).toBeTruthy()
  expect((await comparison.json()).comparable).toBeTruthy()

  const report = await request.get(`/api/scenarios/${scenarioId}/plan-versions/${versions[2].id}/report`)
  expect(report.ok()).toBeTruthy()
  expect(await report.text()).toContain('FieldFlow 调度台')
  const schedules = await (await request.get(`/api/scenarios/${scenarioId}/schedules`)).json()
  expect(schedules.map((item: { version: number }) => item.version)).toEqual([1, 2, 3])
})
