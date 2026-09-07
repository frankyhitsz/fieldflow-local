import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { RuntimeJob } from './types'

const terminal = new Set(['COMPLETED', 'CANCELLED', 'FAILED'])
const labels: Record<string, string> = { QUEUED: '排队中', RUNNING: '运行中', CANCEL_REQUESTED: '正在取消', CANCELLED: '已取消', COMPLETED: '已完成', FAILED: '失败', INTERRUPTED: '中断，等待重派' }
export function JobCenter({ scenarioId, onOpenResult }: { scenarioId: string; onOpenResult: (job: RuntimeJob) => void }) {
  const [jobs, setJobs] = useState<RuntimeJob[]>([])
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [offset, setOffset] = useState(0)
  const [busy, setBusy] = useState<string>()
  const attempts = useRef(new AbortController())
  useEffect(() => { const controller = new AbortController(); attempts.current = controller; return () => controller.abort() }, [])
  useEffect(() => {
    const controller = new AbortController()
    let timer: number | undefined
    let failures = 0
    const poll = async () => {
      try {
        const latest = await api.jobs(scenarioId, controller.signal, offset)
        if (controller.signal.aborted) return
        setJobs(latest); setError(''); failures = 0
      } catch (cause) { if (!controller.signal.aborted) { failures += 1; setError(cause instanceof Error ? cause.message : '任务读取失败') } }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, Math.min(2000 * 2 ** failures, 30000))
    }
    void poll()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [scenarioId, refresh, offset])
  return <section className="page-view job-center"><div className="page-heading"><div><h2>任务中心</h2><p>任务保存在本地服务。刷新页面或离开此页不会取消任务。</p></div><button onClick={() => setRefresh(value => value + 1)}>刷新任务</button></div>
    {error && <p role="alert">{error}；已有记录仅供回看。</p>}
    {!jobs.length && <p>{error ? '暂时无法读取任务' : '本页没有任务'}</p>}
    <div className="table-scroll"><table className="compare-table"><thead><tr><th>任务</th><th>状态 / 进度</th><th>尝试 / 结果</th><th>操作</th></tr></thead><tbody>{jobs.map(job => <tr key={job.id}><td>{job.job_type}<small className="job-id">{job.id}</small></td><td>{labels[job.status] || job.status} · {job.progress}%{job.error && <p>{String(job.error.message || job.error.code || '请重试')}</p>}</td><td>第 {job.attempt_number} 次<small className="job-id">{job.result_resource_id || '尚无结果'}</small></td><td>{!terminal.has(job.status) && <button disabled={!!busy || !!error || job.status === 'CANCEL_REQUESTED'} onClick={async () => { setBusy(job.id); try { await api.cancelJob(scenarioId, job.id); setRefresh(value => value + 1) } catch (cause) { setError(cause instanceof Error ? cause.message : '取消失败') } finally { setBusy(undefined) } }}>取消任务</button>}{job.status === 'COMPLETED' && job.result_resource_id && <button onClick={() => onOpenResult(job)}>查看结果</button>}{job.result_resource_type === 'decision_analysis' && ['FAILED', 'CANCELLED'].includes(job.status) && job.result_resource_id && <button disabled={!!busy} onClick={async () => { setBusy(job.id); try { await api.retryDecisionAnalysisRun(scenarioId, job.result_resource_id!, undefined, attempts.current.signal); setRefresh(value => value + 1) } catch (cause) { setError(cause instanceof Error ? cause.message : '重试失败') } finally { setBusy(undefined) } }}>重试分析</button>}</td></tr>)}</tbody></table></div>
    <div className="editor-actions"><button disabled={offset === 0} onClick={() => setOffset(value => Math.max(0, value - 100))}>上一页</button><span>第 {offset / 100 + 1} 页</span><button disabled={jobs.length < 100} onClick={() => setOffset(value => value + 100)}>下一页</button></div>
  </section>
}
