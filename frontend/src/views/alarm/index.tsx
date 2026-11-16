import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/alarm'
const columns = [
  "规则编号",
  "监控指标",
  "告警条件",
  "告警级别",
  "通知渠道",
  "静默周期",
  "升级策略",
  "规则状态"
]
const judgeColumns = [
  "判定编号",
  "监控指标",
  "指标值",
  "来源标识",
  "唯一生效规则",
  "通知规则",
  "升级原因",
  "通知抑制结果",
  "判定时间"
]
const actions = [
  "启用告警",
  "暂停告警",
  "关闭告警"
]
const statuses = [
  "草稿",
  "已启用",
  "已暂停",
  "已关闭"
]

type Row = Record<string, string | number | null>
type Judgment = Row & { 命中规则?: string[] }

export default function AlarmView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [judgments, setJudgments] = useState<Judgment[]>([])
  const [judgeTotal, setJudgeTotal] = useState(0)
  const [detail, setDetail] = useState<Row | null>(null)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [evalForm, setEvalForm] = useState({ 监控指标: 'latency_p99', 指标值: '950ms', 来源标识: 'JOBO-0002' })
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const stats = useMemo(() => ([
    { label: '告警规则总数', value: total },
    { label: '已启用告警', value: rows.filter((row) => row.status === '已启用').length },
    { label: '判定结果总数', value: judgeTotal },
  ]), [rows, total, judgeTotal])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [ruleResp, judgeResp] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/determinations?size=50`),
      ])
      if (!ruleResp.ok) {
        throw new Error('告警规则列表读取失败')
      }
      const payload = await ruleResp.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (judgeResp.ok) {
        const judgePayload = await judgeResp.json()
        setJudgments(judgePayload.items ?? [])
        setJudgeTotal(judgePayload.total ?? 0)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流告警规则列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('流告警规则动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流告警规则操作失败')
    }
  }

  async function runEvaluate() {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/evaluate`, {
        method: 'POST',
        body: JSON.stringify({ values: evalForm }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '告警判定未生成')
      }
      setNotice(`${payload.message}，通知抑制结果：${payload.entry?.通知抑制结果 ?? '—'}`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '告警判定触发失败')
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) {
        throw new Error('告警规则详情读取失败')
      }
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '告警规则详情读取失败')
    }
  }

  const detailJudgments = (detail?.判定结果列表 as Judgment[] | undefined) ?? []

  return (
    <section className="page" data-module="alarm">
      <header className="page-head">
        <div>
          <h2>流告警规则管理</h2>
          <p className="page-desc">维护流告警的监控指标、触发条件与通知策略；同一监控指标命中多条规则时只生成一份告警判定数据结果。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('告警规则登记入口尚未接入审批流')}>
            登记告警规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出流告警规则清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>最近判定</th>
            <th>通知抑制</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column}>
                  {column === '规则编号' ? (
                    <button className="link" type="button" onClick={() => void openDetail(row)}>
                      {row[column] ?? '—'}
                    </button>
                  ) : (row[column] ?? '—')}
                </td>
              ))}
              <td>{row.最近判定编号 || '—'}</td>
              <td>{row.最近通知抑制结果 || '—'}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无流告警规则数据，可先登记告警规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void runEvaluate() }}>
        <label className="filter-item">
          <span>监控指标</span>
          <input
            value={evalForm.监控指标}
            onChange={(event) => setEvalForm((prev) => ({ ...prev, 监控指标: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>指标值</span>
          <input
            value={evalForm.指标值}
            onChange={(event) => setEvalForm((prev) => ({ ...prev, 指标值: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>来源标识</span>
          <input
            value={evalForm.来源标识}
            onChange={(event) => setEvalForm((prev) => ({ ...prev, 来源标识: event.target.value }))}
          />
        </label>
        <button className="btn primary" type="submit">触发告警判定</button>
        {notice && <span>{notice}</span>}
      </form>

      <h3>告警判定数据结果</h3>
      <table className="data-table">
        <thead>
          <tr>
            {judgeColumns.map((column) => <th key={column}>{column}</th>)}
          </tr>
        </thead>
        <tbody>
          {judgments.map((row) => (
            <tr key={String(row.判定编号 ?? row.id)}>
              {judgeColumns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
            </tr>
          ))}
          {judgments.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={judgeColumns.length}>暂无告警判定数据结果，可先触发告警判定</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="page" data-module="alarm-detail">
          <h3>规则详情：{String(detail.规则编号 ?? '')}</h3>
          <table className="data-table">
            <tbody>
              {columns.map((column) => (
                <tr key={column}>
                  <th>{column}</th>
                  <td>{detail[column] ?? '—'}</td>
                </tr>
              ))}
              <tr>
                <th>最近判定</th>
                <td>{detail.最近判定编号 || '—'}</td>
              </tr>
            </tbody>
          </table>
          <h3>关联判定结果</h3>
          <table className="data-table">
            <thead>
              <tr>
                {judgeColumns.map((column) => <th key={column}>{column}</th>)}
              </tr>
            </thead>
            <tbody>
              {detailJudgments.map((row) => (
                <tr key={String(row.判定编号 ?? row.id)}>
                  {judgeColumns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                </tr>
              ))}
              {detailJudgments.length === 0 && (
                <tr>
                  <td className="empty-state" colSpan={judgeColumns.length}>该规则暂无关联判定结果</td>
                </tr>
              )}
            </tbody>
          </table>
          <footer className="page-foot">
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>收起详情</button>
          </footer>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条流告警规则记录，{judgeTotal} 条告警判定数据结果</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
