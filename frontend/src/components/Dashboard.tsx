import { useEffect, useState } from 'react';
import { api } from '../api';
import type { Dispatch, SetStateAction } from 'react';
import type { DmrException, DmrRecord, LibraryFile, ViewName } from '../types';
import { brief, MetricStrip, modules, projectCategories, SelectFilter } from './shared';

type Finder = { registrant: string; module: string; product: string; projectCategory: string; query: string };
type CatalogHealth = { label: string; detail: string; warning: boolean };
type Props = {
  active?: boolean;
  records: DmrRecord[];
  files: LibraryFile[];
  exceptions: DmrException[];
  todayUpdateCount: number;
  finder: Finder;
  setFinder: Dispatch<SetStateAction<Finder>>;
  finderResults?: DmrRecord[];
  onSearch?: (queryOverride?: string) => void;
  openRecord: (id: string) => void;
  setView: (view: ViewName) => void;
  runSync: () => void;
  canSync: boolean;
  setExceptionId: (id: string) => void;
};

export function Dashboard({ active = true, records, files, exceptions, todayUpdateCount, finder, setFinder, onSearch, openRecord, setView, runSync, canSync, setExceptionId }: Props) {
  const [catalogOptions, setCatalogOptions] = useState<Array<{registrant: string; product: string; projectCategory: string}>>([]);
  const [catalogHealth, setCatalogHealth] = useState<CatalogHealth>({ label: '读取中', detail: '正在读取飞书清单状态', warning: true });
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    api('/api/dmr-catalog/refresh?force=true&wait=true', {}).then(() => api<{documents: Array<{name: string; path: string[]}>; snapshot_stale?: boolean; sync_status?: {status?: string; message?: string}}>('/api/dmr-catalog')).then(result => {
      if (cancelled) return;
      setCatalogOptions(result.documents.map(doc => ({ registrant: doc.name.split(/[-–—]DMR/i)[0], product: (doc.path[2] || '').replace(/^\d+[.、]/, ''), projectCategory: doc.path.some(part => part.includes('医疗')) ? '医疗' : doc.path.some(part => part.includes('家电')) ? '家电' : '待确认' })));
      const failed = result.sync_status?.status === 'failed';
      const stale = Boolean(result.snapshot_stale);
      setCatalogHealth({
        label: failed ? '同步异常' : stale ? '快照过期' : '已更新',
        detail: failed ? (result.sync_status?.message || '目录刷新失败，当前显示旧快照') : stale ? '清单快照超过 24 小时，请检查目录刷新' : '目录快照在 24 小时内更新',
        warning: failed || stale,
      });
    }).catch(() => { if (!cancelled) { setCatalogOptions([]); setCatalogHealth({ label: '读取失败', detail: '无法读取飞书清单状态，请检查权限或连接', warning: true }); } });
    return () => { cancelled = true; };
  }, [active]);
  const moduleTotals = modules.map(name => [name, records.filter(record => record.module === name).length] as const);
  const max = Math.max(1, ...moduleTotals.map(([, count]) => count));
  return <section className={`app-view ${active ? 'active' : ''}`} id="view-dashboard" data-title="工作台">
    <div className="page-heading">
      <div><p className="eyebrow">DMR CONTROL DESK · LOCAL</p><h1>量产 DMR 文件</h1><p>查找本地当前版本，查看文件归档和待核对情况。</p></div>
      {canSync && <div className="heading-actions"><button className="primary-button" data-action="refresh-data" onClick={runSync}><svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6" /></svg>检查本地数据</button></div>}
    </div>
    <section className="finder-card">
      <div className="finder-title"><span className="finder-icon"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /></svg></span><div><strong>查找 DMR 云清单</strong><small>按主体、项目类别、机型和模块查找清单；支持文件编号、文件名称模糊搜索</small></div></div>
      <div className="finder-form">
        <SelectFilter id="finderRegistrant" value={finder.registrant} label="全部主体" values={catalogOptions.map(record => record.registrant)} onChange={value => setFinder(item => ({ ...item, registrant: value, product: 'all' }))} />
        <SelectFilter id="finderProjectCategory" value={finder.projectCategory} label="全部项目类别" values={projectCategories} onChange={value => setFinder(item => ({ ...item, projectCategory: value, product: 'all' }))} />
        <SelectFilter id="finderProduct" value={finder.product} label="全部机型" values={catalogOptions.filter(record => (finder.registrant === 'all' || record.registrant === finder.registrant) && (finder.projectCategory === 'all' || record.projectCategory === finder.projectCategory)).map(record => record.product)} onChange={value => setFinder(item => ({ ...item, product: value }))} />
        <SelectFilter id="finderModule" value={finder.module} label="全部模块" values={modules} onChange={value => setFinder(item => ({ ...item, module: value }))} />
        <label><input id="finderQuery" type="search" value={finder.query} aria-label="搜索 DMR 云清单" placeholder="文件编号或文件名称，可输入多个关键词" onChange={event => { const query = event.currentTarget.value; setFinder(item => ({ ...item, query })); }} onKeyDown={event => { if (event.key === 'Enter') onSearch?.(); }} /><button id="finderButton" onClick={() => onSearch?.()}>查询</button></label>
      </div>
      <div className="finder-shortcuts"><span>常用：</span>{['UI20S', 'W20', 'BOM', '软件'].map(value => <button key={value} data-quick-search={value} onClick={() => { setFinder({ registrant: 'all', module: 'all', product: 'all', projectCategory: 'all', query: value }); onSearch?.(value); }}>{value}</button>)}</div>
    </section>
    <MetricStrip id="dashboardMetrics" items={[[records.length, '当前有效记录'], [todayUpdateCount, '今日归档更新'], [files.length, '本地附件（含历史）'], [exceptions.length, '待处理异常']]} />
    <div className="dashboard-grid">
      <div className="dashboard-column">
      <section className="panel updates-panel">
        <div className="panel-head"><div><p className="eyebrow">LATEST UPDATES</p><h2>最近版本更新</h2></div><button className="text-button" data-view-target="register" onClick={() => setView('register')}>查看完整清单 →</button></div>
        <div className="table-scroll" role="region" aria-label="最近版本更新列表" tabIndex={0}><table><thead><tr><th>文件</th><th>机型 / 模块</th><th>版本变化</th><th>关联附件</th><th>更新时间</th><th /></tr></thead><tbody id="recentUpdatesBody">
          {records.length ? records.slice(0, 5).map(record => <tr key={record.id}>
            <td><span className="cell-main">{record.no}</span><span className="cell-sub">{record.name}</span></td>
            <td><span className="cell-main">{record.product}</span><span className="cell-sub">{record.module}</span></td>
            <td><span className="version-badge">{record.previous || '新建'} → {record.version}</span></td>
            <td><span className="file-link">{record.file}</span></td><td>{record.date}</td>
            <td><button className="row-button" data-record={record.id} onClick={() => openRecord(record.id)}>查看</button></td>
          </tr>) : <tr><td colSpan={6} className="empty-row">暂无资料，请从批量录入上传真实文件。</td></tr>}
        </tbody></table></div>
      </section>
      <section className="panel module-panel">
        <div className="panel-head"><div><p className="eyebrow">FILE COVERAGE</p><h2>文件模块</h2></div><span className="small-chip">7个模块</span></div>
        <div className="module-list" id="moduleList">{moduleTotals.map(([name, count]) => <div className={`module-item ${count ? '' : 'is-empty'}`} key={name}><div><span>{name}</span><strong>{count}</strong></div><i><b style={{ width: `${count / max * 100}%` }} /></i></div>)}</div>
      </section>
      </div>
      <div className="dashboard-column">
      <section className="panel source-health-panel">
        <div className="panel-head"><div><p className="eyebrow">DATA STATUS</p><h2>数据同步状态</h2></div><span className={`status-chip ${catalogHealth.warning ? 'warning' : 'success'}`}>{catalogHealth.warning ? '需关注' : '正常'}</span></div>
        <div className="source-health" id="sourceHealth" role="region" aria-label="数据同步状态列表" tabIndex={0}><div className="source-health-row"><i /><div><strong>本地数据库与附件</strong><small>{records.length} 条记录 / {files.length} 个附件</small></div><time>已连接</time></div><div className={`source-health-row ${catalogHealth.warning ? 'is-warning' : ''}`}><i /><div><strong>飞书清单目录</strong><small title={catalogHealth.detail}>{brief(catalogHealth.detail, 55)}；云端回写未开启</small></div><time>{catalogHealth.label}</time></div></div>
        <button className="panel-footer-button" data-view-target="logs" onClick={() => setView('logs')}>查看日志记录</button>
      </section>
      <section className="panel exception-panel">
        <div className="panel-head"><div><p className="eyebrow">NEEDS ATTENTION</p><h2>需要文控处理</h2></div><span className="count-chip" id="exceptionCount">{exceptions.length}</span></div>
        <div className="exception-list" id="exceptionList" role="region" aria-label="需要文控处理列表" tabIndex={0}>{exceptions.length ? exceptions.map(item => <div className="exception-item" key={item.id}><i>!</i><div style={{ minWidth: 0 }}><strong title={item.title} style={{ overflowWrap: 'anywhere' }}>{brief(item.recordNo || item.title, 24)}</strong><small title={item.detail} style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{brief(item.detail, 50)}</small></div><button data-exception={item.id} onClick={() => setExceptionId(item.id)}>处理</button></div>) : <div className="empty-exception"><strong>暂无待处理异常</strong><small>仅反映已导入本地的资料。</small></div>}</div>
      </section>
      </div>
    </div>
  </section>;
}
