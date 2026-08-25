import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('API write contracts', () => {
  it('does not send aggregate identity or execution status in a work-order update', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ id: 'main' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await api.updateWorkOrder('main', 'WO-1', {
      id: 'WO-1',
      status: 'started',
      note: '客户确认新备注',
    }, 7)

    expect(fetchMock).toHaveBeenCalledOnce()
    const [path, request] = fetchMock.mock.calls[0]
    expect(path).toBe('/api/v2/scenarios/main/work-orders/WO-1')
    expect(request?.headers).toMatchObject({ 'If-Match': 'D7' })
    expect(JSON.parse(String(request?.body))).toEqual({ note: '客户确认新备注' })
  })

  it('preserves structured conflict codes and diagnostics', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      detail: {
        code: 'ACTIVE_PLAN_CHANGED_DURING_COMMAND',
        message: '活动方案已变化，请重新运行',
        expected_active_plan_id: 'PV-1',
        current_active_plan_id: 'PV-2',
      },
    }), { status: 409, headers: { 'Content-Type': 'application/json' } })))

    const failure = await api.optimize('main').catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(ApiError)
    expect(failure).toMatchObject({
      status: 409,
      code: 'ACTIVE_PLAN_CHANGED_DURING_COMMAND',
      message: '活动方案已变化，请重新运行',
      details: {
        expected_active_plan_id: 'PV-1',
        current_active_plan_id: 'PV-2',
      },
    })
  })
})
