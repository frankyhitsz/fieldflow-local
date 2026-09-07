import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api } from '../api'
import { JobCenter } from '../JobCenter'
import type { RuntimeJob } from '../types'

const job: RuntimeJob = {
  id: 'JOB-PERSISTED', job_type: 'COST_ANALYSIS', scenario_id: 'main', status: 'RUNNING', progress: 35,
  input_payload: {}, input_manifest_hash: 'input', dedupe_key: 'persisted-command', attempt_number: 1,
  lease_owner: 'worker', lease_expires_at: '2026-09-07T15:00:00Z', heartbeat_at: '2026-09-07T14:00:00Z',
  result_resource_type: null, result_resource_id: null, error: null,
  created_at: '2026-09-07T14:00:00Z', updated_at: '2026-09-07T14:00:00Z', finished_at: null,
}
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('durable task center', () => {
  it('finds an existing task after remount and can cancel it', async () => {
    let current = job
    vi.spyOn(api, 'jobs').mockImplementation(async () => [current])
    const cancel = vi.spyOn(api, 'cancelJob').mockImplementation(async () => { current = { ...job, status: 'CANCELLED', progress: 100 }; return current })
    const first = render(<JobCenter scenarioId="main" onOpenResult={() => undefined} />)
    expect(await screen.findByText('JOB-PERSISTED')).toBeInTheDocument()
    first.unmount()
    render(<JobCenter scenarioId="main" onOpenResult={() => undefined} />)
    await screen.findByText('JOB-PERSISTED')
    fireEvent.click(screen.getByRole('button', { name: '取消任务' }))
    await waitFor(() => expect(cancel).toHaveBeenCalledWith('main', 'JOB-PERSISTED'))
    expect(await screen.findByText(/已取消 · 100%/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '查看结果' })).not.toBeInTheDocument()
  })

  it('opens exactly the persisted result and never cancels a completed task', async () => {
    const completed: RuntimeJob = { ...job, status: 'COMPLETED', result_resource_type: 'plan_version', result_resource_id: 'PV-FROZEN-RESULT' }
    vi.spyOn(api, 'jobs').mockResolvedValue([completed])
    const open = vi.fn()
    render(<JobCenter scenarioId="main" onOpenResult={open} />)
    await screen.findByText('PV-FROZEN-RESULT')
    fireEvent.click(screen.getByRole('button', { name: '查看结果' }))
    expect(open).toHaveBeenCalledWith(completed)
    expect(screen.queryByRole('button', { name: '取消任务' })).not.toBeInTheDocument()
  })
})
