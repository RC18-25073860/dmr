import { CatalogSearchResults, type CatalogSearch } from './CatalogSearchResults';
import type { Dispatch, SetStateAction } from 'react';
import type { DmrRecord, LibraryFile } from '../types';
import { CloudCatalog } from './CloudCatalog';
import { CatalogUpdates } from './CatalogUpdates';
import { MetricStrip, modules, projectCategory, projectCategories, RecordRow, SelectFilter } from './shared';

type Filter = { q: string; registrant: string; type: string; module: string; product: string };
type Props = {
  active?: boolean;
  catalogSearch?: CatalogSearch | null;
  clearCatalogSearch?: () => void;
  records: DmrRecord[];
  files?: LibraryFile[];
  filtered: DmrRecord[];
  filter: Filter;
  setFilter: Dispatch<SetStateAction<Filter>>;
  openRecord: (id: string) => void;
  runSync: () => void;
  canSync: boolean;
  todayUpdateCount?: number;
};

export function Register({ catalogSearch, clearCatalogSearch, active = true, records, filtered, filter, setFilter, openRecord, runSync, canSync }: Props) {
  return <section className={`app-view ${active ? 'active' : ''}`} id="view-register" data-title="DMR 清单">
    <div className="page-heading compact"><div><p className="eyebrow">MASTER REGISTER</p><h1>量产 DMR 清单</h1><p>这里展示飞书原始清单及文控待办；本机归档文件在「受控文件」中查看。</p></div>{canSync && <div className="heading-actions"><button className="primary-button" data-action="refresh-data" onClick={runSync}>检查本地数据</button></div>}</div>
    {catalogSearch && <CatalogSearchResults search={catalogSearch} onClear={() => clearCatalogSearch?.()} />}
    <div className="register-cloud-intro"><strong>飞书云端清单</strong><span>归档到本机不会改动云端清单。下方提醒提供关联清单和待核对内容，由文控打开原文手动处理。</span></div>
    <CatalogUpdates active={active} />
    <CloudCatalog active={active} />

    <details className="local-register-details">
      <summary><div><strong>本地已归档文件 · {records.length} 条记录</strong><span>点击展开查询本机文件；与上方飞书云端清单分开保存，文件变更可到日志记录查看。</span></div></summary>
      <div className="local-register-content">
        <MetricStrip id="registerMetrics" items={[[records.length, '本地有效记录'], [new Set(records.map(record => record.registrant)).size, '注册人主体'], [new Set(records.map(record => JSON.stringify([record.registrant, record.product]))).size, '主体 / 机型'], [modules.length, '标准文件模块']]} />
        <section className="table-panel">
          <div className="table-toolbar register-toolbar"><label className="table-search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /></svg><input id="registerSearch" type="search" aria-label="搜索本地归档文件" placeholder="搜索文件名、SOP、编号、机型或版本" value={filter.q} onChange={event => { const q = event.currentTarget.value; setFilter(item => ({ ...item, q })); }} /></label><div>
            <SelectFilter id="registerRegistrantFilter" value={filter.registrant} label="全部主体" values={records.map(record => record.registrant)} onChange={value => setFilter(item => ({ ...item, registrant: value, product: 'all' }))} />
            <SelectFilter id="registerTypeFilter" value={filter.type} label="全部项目类别" values={projectCategories} onChange={value => setFilter(item => ({ ...item, type: value, product: 'all' }))} />
            <SelectFilter id="registerProductFilter" value={filter.product} label="全部机型" values={records.filter(record => (filter.registrant === 'all' || record.registrant === filter.registrant) && (filter.type === 'all' || projectCategory(record) === filter.type)).map(record => record.product)} onChange={value => setFilter(item => ({ ...item, product: value }))} />
            <SelectFilter id="registerModuleFilter" value={filter.module} label="全部模块" values={modules} onChange={value => setFilter(item => ({ ...item, module: value }))} />
          </div></div>
          <p className="register-result-count" role="status">{filter.product === 'all' ? '全部机型' : filter.product} · 找到 {filtered.length} 条当前有效记录</p>
          <div className="table-scroll"><table><thead><tr><th>文件编号 / 名称</th><th>注册人主体</th><th>项目类别</th><th>机型</th><th>模块</th><th>当前版本</th><th>受控文件</th><th>最后更新</th><th /></tr></thead><tbody id="registerTableBody">{filtered.length ? filtered.map(record => <RecordRow key={record.id} record={record} openRecord={openRecord} />) : <tr><td colSpan={9} className="empty-row">没有符合条件的记录。</td></tr>}</tbody></table></div>
        </section>
      </div>
    </details>
  </section>;
}
