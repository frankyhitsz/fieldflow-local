import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'

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
})
