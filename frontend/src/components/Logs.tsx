import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react';
import type { DmrException, OperationLog, SyncChange } from '../types';
import { api, apiErrorMessage } from '../api';
import { MetricStrip, SelectFilter } from './shared';

const todayText = () => new Date().toLocaleDateString('sv-SE');

type TransferManifest = {
  id: string;
  filename: string;
  file_date: string;
  target_path: string;
  network_path: string;
  status: string;
  last_error: string;
  updated_at: string;
};

const logScopes = [
  ['all', '全部记录'],
  ['sync', '同步批次'],
  ['manual', '人工操作'],
  ['system', '系统任务'],
] as const;

export function Logs({
  logs: initialLogs,
  allLogs,
  exceptions,
  filter,
  setFilter,
  changes,
  latest,
  confirmLatestSync,
  runSync,
  canSync,
  active = true,
}: {
  logs: OperationLog[];
  allLogs: OperationLog[];
  exceptions: DmrException[];
  filter: { q: string; user: string; action: string; scope: string };
  setFilter: Dispatch<SetStateAction<{ q: string; user: string; action: string; scope: string }>>;
  changes: SyncChange[];
  latest: { time: string; batch: string; confirmed: boolean; by: string };
  confirmLatestSync: () => Promise<void>;
  runSync: () => void;
  canSync: boolean;
  active?: boolean;
}) {
  const [page, setPage] = useState(0);
  const [remote, setRemote] = useState<{items: OperationLog[]; total: number; has_more: boolean; users: string[]; actions: string[]; stats: {today: number; sync: number; manual: number}} | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [transferManifests, setTransferManifests] = useState<TransferManifest[]>([]);
  const filterStamp = JSON.stringify(filter);
  useEffect(() => { setPage(0); }, [filterStamp]);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    setLoading(true); setError('');
    const timer = setTimeout(() => {
      const query = new URLSearchParams({...filter, offset: String(page * 100), limit: '100'});
      api<NonNullable<typeof remote>>('/api/logs?' + query).then(value => { if (!cancelled) setRemote(value); })
        .catch(reason => { if (!cancelled) {setRemote(null); setError(apiErrorMessage(reason));} })
        .finally(() => { if (!cancelled) setLoading(false); });
    }, 200);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [active, filterStamp, page, allLogs]);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    api<{ items: TransferManifest[] }>('/api/integrations/mail/transfer-manifests')
      .then(value => { if (!cancelled) setTransferManifests(value.items); })
      .catch(() => { if (!cancelled) setTransferManifests([]); });
    return () => { cancelled = true; };
  }, [active, allLogs]);
  const logs = remote?.items || (error ? [] : initialLogs);
  const today = todayText();
  const filterKey = JSON.stringify(filter);
  const hasFilter = Boolean(filter.q.trim() || filter.user !== 'all' || filter.action !== 'all' || filter.scope !== 'all');
  const [expanded, setExpanded] = useState<{ filterKey: string; days: Record<string, boolean> }>({ filterKey: '', days: {} });
  const days = useMemo(() => {
    const grouped = new Map<string, OperationLog[]>();
    for (const log of logs) {
      const match = log.time.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T]|$)/);
      const day = match ? `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}` : '日期未知';
      const entries = grouped.get(day) || [];
      entries.push(log);
      grouped.set(day, entries);
    }
    return [...grouped.entries()].sort(([a], [b]) => a === '日期未知' ? 1 : b === '日期未知' ? -1 : b.localeCompare(a));
  }, [logs]);
  const defaultDay = days.some(([day]) => day === today) ? today : days[0]?.[0];
  const isExpanded = (day: string) => (expanded.filterKey === filterKey ? expanded.days[day] : undefined) ?? (hasFilter || day === defaultDay);
  const toggleDay = (day: string) => setExpanded({ filterKey, days: { ...(expanded.filterKey === filterKey ? expanded.days : {}), [day]: !isExpanded(day) } });
  const setAllDays = (open: boolean) => setExpanded({ filterKey, days: Object.fromEntries(days.map(([day]) => [day, open])) });

  return (
    <section className={`app-view ${active ? 'active' : ''}`} id="view-logs" data-title="日志记录">
      <div className="page-heading compact">
        <div>
          <p className="eyebrow">ACTIVITY & AUDIT TRAIL</p>
          <h1>日志记录</h1>
          <p>文件变更、本地检查和人员操作统一留痕，可按日期、操作人和关键词查询。</p>
        </div>
        {canSync && <div className="heading-actions">
          <button className="primary-button" data-action="refresh-data" onClick={runSync}>检查本地数据</button>
        </div>}
      </div>

      <MetricStrip
        id="logMetrics"
        items={[
          [remote?.stats.today ?? allLogs.filter(item => item.time.startsWith(todayText())).length, '今日记录'],
          [remote?.stats.sync ?? allLogs.filter(item => item.action === '同步数据').length, '本地检查'],
          [remote?.stats.manual ?? allLogs.filter(item => item.user !== '系统自动任务').length, '人工操作'],
          [exceptions.length, '待处理异常'],
        ]}
      />

      <details className="table-panel transfer-manifest-panel">
        <summary>DMR 转移清单归档 · {transferManifests.length} 条最近记录</summary>
        <div className="table-scroll">
          <table><thead><tr><th>文件</th><th>文件日期</th><th>192 目标目录</th><th>状态</th></tr></thead><tbody>
            {transferManifests.map(item => <tr key={item.id}>
              <td>{item.filename}</td><td>{item.file_date}</td><td>{item.network_path || item.target_path}</td>
              <td>{item.status === 'archived' ? '已归档' : item.status === 'staged' ? '平台已暂存，未写入192' : item.status === 'needs_review' ? `同名冲突，待人工核对；未覆盖192原文件。${item.last_error}` : `待重试：${item.last_error || '尚未写入192'}`}</td>
            </tr>)}
            {!transferManifests.length && <tr><td colSpan={4}>暂无转移清单接收记录</td></tr>}
          </tbody></table>
        </div>
      </details>

      <section className="sync-batch-card" id="syncLatestBatch">
        <div className="sync-batch-head">
          <div>
            <span className="batch-check">✓</span>
            <div>
              <strong>最近一次本地检查</strong>
              <small>{latest.time || '尚未执行'} · {latest.batch}</small>
            </div>
          </div>
          <div className="batch-actions">
            <span>{changes.length} 项检查结果</span>
            {canSync && latest.batch && !latest.confirmed ? (
              <button data-action="confirm-sync" onClick={() => void confirmLatestSync()}>确认本次检查</button>
            ) : (
              <em>{latest.by}</em>
            )}
          </div>
        </div>
        <div className="sync-schedule">
          <div>
            <i />
            <strong>云端回写尚未开启</strong>
            <span>飞书清单仅展示只读快照；DCC 邮件、PLM、定时同步与外部消息发送未启用</span>
          </div>
          <b>本地检查可用</b>
        </div>
        <div className="sync-rule">
          <i>i</i>
          <span>检查本地附件完整性与 BOM 关联；确认仅记录本地核对，不代表外部审批。</span>
        </div>
        <div className="sync-change-grid">
          {changes.length ? changes.map((change, index) => (
            <article className={`sync-change ${change.status === 'warning' ? 'warning' : ''}`} key={`${change.source || '本地'}-${change.object || index}-${index}`}>
              <header>
                <span>{change.source || '本地'}</span>
                <b>{change.result}</b>
              </header>
              <strong>{change.object}</strong>
              <p>{change.detail}</p>
            </article>
          )) : (
            <div className="sync-empty">
              <strong>可点击“检查本地数据”检查本地资料</strong>
              <span>不会模拟外部系统成功。</span>
            </div>
          )}
        </div>
      </section>

      <section className="table-panel">
        <div className="segment-tabs log-scope-tabs">
          {logScopes.map(([scope, label]) => (
            <button
              className={filter.scope === scope ? 'active' : ''}
              data-log-scope={scope}
              key={scope}
              onClick={() => setFilter(item => ({ ...item, scope }))}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="table-toolbar">
          <label className="table-search">
            <svg viewBox="0 0 24 24">
              <circle cx="11" cy="11" r="6" />
              <path d="m16 16 4 4" />
            </svg>
            <input
              id="logSearch"
              type="search"
              placeholder="搜索用户、文件编号或操作"
              value={filter.q}
              onChange={event => { const q = event.currentTarget.value; setFilter(item => ({ ...item, q })); }}
            />
          </label>
          <div>
            <SelectFilter
              id="logUserFilter"
              value={filter.user}
              label="全部用户"
              values={remote?.users || allLogs.map(item => item.user)}
              onChange={value => setFilter(item => ({ ...item, user: value }))}
            />
            <SelectFilter
              id="logActionFilter"
              value={filter.action}
              label="全部操作"
              values={remote?.actions || allLogs.map(item => item.action)}
              onChange={value => setFilter(item => ({ ...item, action: value }))}
            />
          </div>
        </div>
        <div className="log-day-toolbar">
          <span>共 {remote?.total ?? logs.length} 条 · 本页 {logs.length} 条 · {days.length} 天{hasFilter ? ' · 筛选结果' : ''}</span>
          <div><button className="row-button" disabled={!days.length} onClick={() => setAllDays(true)}>展开全部</button><button className="row-button" disabled={!days.length} onClick={() => setAllDays(false)}>收起全部</button></div>
        </div>
        {error && <p role="alert">日志读取失败：{error}</p>}
        {loading && <p role="status">正在读取日志…</p>}
        <div className="log-day-toolbar"><span>第 {page + 1} 页，每页最多 100 条</span><div><button className="row-button" disabled={loading || page === 0} onClick={() => setPage(value => value - 1)}>上一页</button><button className="row-button" disabled={loading || !remote?.has_more} onClick={() => setPage(value => value + 1)}>下一页</button></div></div>
        <div className="log-day-groups" id="logTableBody">
          {days.length ? days.map(([day, entries]) => {
            const open = isExpanded(day);
            const panelId = `log-day-${day}`;
            return <section className="log-day-group" key={day}>
              <button className="log-day-toggle" aria-expanded={open} aria-controls={panelId} onClick={() => toggleDay(day)}>
                <svg viewBox="0 0 24 24" aria-hidden="true" className={open ? 'expanded' : ''}><path d="m9 5 7 7-7 7" /></svg>
                <strong>{day}{day === today ? ' · 今天' : ''}</strong><span className="log-day-count">{entries.length} 条</span><span className="log-day-action">{open ? '收起' : '展开'}</span>
              </button>
              <div className="table-scroll" id={panelId} hidden={!open}>
                {open && <table>
                  <thead><tr><th>时间</th><th>操作人</th><th>操作</th><th>业务对象</th><th>变更内容</th><th>来源 / 批次</th><th>结果</th></tr></thead>
                  <tbody>{entries.map((log, index) => <tr key={`${log.time}-${index}`}>
                    <td><span className="cell-main">{log.time}</span></td>
                    <td><span className="user-pill" title={log.user}><i aria-hidden="true">{log.user?.slice(0, 1)}</i><strong>{log.user}</strong></span></td>
                    <td><span className="module-badge">{log.action}</span></td>
                    <td><span className="cell-main">{log.object}</span></td>
                    <td><span className="change-text">{log.change}</span></td>
                    <td><span className="trace-id">{log.source}</span></td>
                    <td><span className="status-badge">{log.result}</span></td>
                  </tr>)}</tbody>
                </table>}
              </div>
            </section>;
          }) : <div className="empty-row">没有符合条件的日志。</div>}
        </div>
      </section>
    </section>
  );
}
