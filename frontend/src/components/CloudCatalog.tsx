import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api, apiErrorMessage } from '../api';
import './CloudCatalog.css';

type CloudFolder = {
  token: string;
  parent_token: string | null;
  name: string;
  path: string[];
};

type CloudDocumentSummary = {
  token: string;
  parent_token: string;
  name: string;
  url: string;
  path: string[];
  type: 'sheet';
  revision?: string | null;
  read_at?: string | null;
  sheet_count: number;
};

type CatalogRefreshStatus = {
  status?: string;
  running?: boolean;
  started_at?: string | null;
  finished_at?: string | null;
  checked_at?: string | null;
  last_success_at?: string | null;
  last_error?: string | null;
  message?: string | null;
};

type CloudCatalogSummary = {
  schema_version: 1;
  root_token: string;
  root_url: string;
  fetched_at: string;
  writeback_enabled: false;
  snapshot_stale?: boolean;
  sync_status?: CatalogRefreshStatus;
  refresh?: CatalogRefreshStatus;
  refresh_status?: CatalogRefreshStatus;
  folders: CloudFolder[];
  documents: CloudDocumentSummary[];
};

type LoadState = 'idle' | 'loading' | 'ready' | 'error';
const rootFolderToken = 'ROOT';

function isAbortLike(error: unknown) {
  return error instanceof Error && error.name === 'AbortError';
}

function formatTime(value?: string | null) {
  if (!value) return '尚未读取';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

function pathText(path: string[]) {
  return path.filter(Boolean).join(' / ');
}

function refreshStatus(summary?: CloudCatalogSummary | null) {
  return summary?.refresh_status || summary?.refresh || summary?.sync_status || null;
}

function extractRefreshResponseStatus(result: CatalogRefreshStatus | { refresh_status?: CatalogRefreshStatus }) {
  if ('refresh_status' in result && result.refresh_status) return result.refresh_status;
  return result as CatalogRefreshStatus;
}


function isRefreshRunning(status?: CatalogRefreshStatus | null) {
  if (!status) return false;
  return Boolean(status.running) || ['queued', 'pending', 'running', 'syncing', 'refreshing'].includes(String(status.status || '').toLowerCase());
}

function refreshFailed(status?: CatalogRefreshStatus | null) {
  if (!status) return false;
  return ['failed', 'error'].includes(String(status.status || '').toLowerCase()) || Boolean(status.last_error);
}

function lastSuccessTime(summary?: CloudCatalogSummary | null) {
  const status = refreshStatus(summary);
  return status?.last_success_at || status?.finished_at || summary?.fetched_at || null;
}

function refreshStatusText(summary?: CloudCatalogSummary | null, fallbackError = '') {
  const status = refreshStatus(summary);
  if (isRefreshRunning(status)) return status?.message || '正在同步飞书目录，稍后会自动更新。';
  if (refreshFailed(status)) return status?.last_error || status?.message || fallbackError || '目录同步失败，请稍后重试。';
  return '目录已同步；定位时会重新读取清单单元格。';
}

function includesQuery(values: Array<string | undefined | null>, query: string) {
  if (!query) return true;
  const needle = query.toLocaleLowerCase();
  return values.some(value => String(value || '').toLocaleLowerCase().includes(needle));
}

function folderDepth(folder: CloudFolder) {
  return Math.max(0, folder.path.length - 1);
}

function buildChildren<T extends { parent_token: string | null }>(items: T[]) {
  return items.reduce((map, item) => {
    const key = item.parent_token || rootFolderToken;
    const list = map.get(key) || [];
    list.push(item);
    map.set(key, list);
    return map;
  }, new Map<string, T[]>());
}

function recursiveDocumentCount(folder: CloudFolder, folderChildren: Map<string, CloudFolder[]>, documentChildren: Map<string, CloudDocumentSummary[]>): number {
  const direct = documentChildren.get(folder.token)?.length || 0;
  return direct + (folderChildren.get(folder.token) || []).reduce((sum, child) => sum + recursiveDocumentCount(child, folderChildren, documentChildren), 0);
}

function TreeChevron({ expanded }: { expanded: boolean }) {
  return <svg aria-hidden="true" className={expanded ? 'expanded' : ''} viewBox="0 0 24 24"><path d="m9 6 6 6-6 6" /></svg>;
}

function SearchIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /></svg>;
}

export function CloudCatalog({ active }: { active: boolean }) {
  const [summary, setSummary] = useState<CloudCatalogSummary | null>(null);
  const [summaryState, setSummaryState] = useState<LoadState>('idle');
  const [summaryError, setSummaryError] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [query, setQuery] = useState('');
  const summaryRequestId = useRef(0);
  const refreshRequestId = useRef(0);
  const initializedTree = useRef(false);
  const lastSuccessBroadcast = useRef<string | null>(null);
  const refreshInFlight = useRef(false);
  const [refreshStarting, setRefreshStarting] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const [postedRefreshStatus, setPostedRefreshStatus] = useState<CatalogRefreshStatus | null>(null);

  const folderChildren = useMemo(() => buildChildren(summary?.folders || []), [summary]);
  const documentChildren = useMemo(() => buildChildren(summary?.documents || []), [summary]);
  const rootFolders = useMemo(() => {
    if (!summary) return [];
    return summary.folders.filter(folder => folder.parent_token === summary.root_token);
  }, [summary]);
  const productFolderCount = useMemo(() => {
    if (!summary) return 0;
    return summary.folders.filter(folder => folder.parent_token && folder.parent_token !== summary.root_token && folder.path.length >= 3).length;
  }, [summary]);

  const matchedDocumentTokens = useMemo(() => {
    if (!summary) return new Set<string>();
    return new Set(summary.documents.filter(document => includesQuery([document.name, pathText(document.path), document.token], query.trim())).map(document => document.token));
  }, [summary, query]);

  const visibleFolderTokens = useMemo(() => {
    const visible = new Set<string>();
    if (!summary) return visible;
    if (!query.trim()) {
      summary.folders.forEach(folder => visible.add(folder.token));
      return visible;
    }
    const folderByToken = new Map(summary.folders.map(folder => [folder.token, folder]));
    for (const document of summary.documents) {
      if (!matchedDocumentTokens.has(document.token)) continue;
      let parent = document.parent_token ? folderByToken.get(document.parent_token) : undefined;
      while (parent) {
        visible.add(parent.token);
        parent = parent.parent_token ? folderByToken.get(parent.parent_token) : undefined;
      }
    }
    summary.folders.filter(folder => includesQuery([folder.name, pathText(folder.path)], query.trim())).forEach(folder => visible.add(folder.token));
    return visible;
  }, [matchedDocumentTokens, query, summary]);


  const loadSummary = ({ quiet = false }: { quiet?: boolean } = {}) => {
    const requestId = ++summaryRequestId.current;
    if (!quiet) setSummaryState('loading');
    setSummaryError('');
    return api<CloudCatalogSummary>('/api/dmr-catalog')
      .then(result => {
        if (requestId !== summaryRequestId.current) return;
        setSummary(result);
        setSummaryState('ready');
        if (!initializedTree.current) {
          setExpanded(new Set(result.folders.filter(folder => folder.path.length <= 2).map(folder => folder.token)));
          initializedTree.current = true;
        }
        const status = refreshStatus(result);
        setPostedRefreshStatus(status);
        const successTime = lastSuccessTime(result);
        if (successTime && successTime !== lastSuccessBroadcast.current && !isRefreshRunning(status) && !refreshFailed(status)) {
          lastSuccessBroadcast.current = successTime;
          window.dispatchEvent(new CustomEvent('dmr-catalog-refreshed'));
        }
      })
      .catch(error => {
        if (isAbortLike(error) || requestId !== summaryRequestId.current) return;
        if (!quiet || !summary) setSummaryState('error');
        setSummaryError(error instanceof ApiError && error.status === 403 ? '当前账号没有读取 DMR 云清单的权限。请开通清单目录读取范围后重试。' : apiErrorMessage(error));
      });
  };

  const startRefresh = ({ manual = false }: { manual?: boolean } = {}) => {
    if (refreshInFlight.current) return Promise.resolve();
    refreshInFlight.current = true;
    const requestId = ++refreshRequestId.current;
    setRefreshStarting(true);
    setRefreshError('');
    return api<CatalogRefreshStatus | { refresh_status?: CatalogRefreshStatus }>('/api/dmr-catalog/refresh?force=true', {})
      .then(result => {
        setPostedRefreshStatus(extractRefreshResponseStatus(result));
        return loadSummary({ quiet: true });
      })
      .catch(error => {
        if (requestId !== refreshRequestId.current) return;
        const message = apiErrorMessage(error);
        setRefreshError(message);
        if (manual) setSummaryError(message);
      })
      .finally(() => {
        refreshInFlight.current = false;
        if (requestId === refreshRequestId.current) setRefreshStarting(false);
      });
  };

  useEffect(() => {
    if (!active) return;
    void loadSummary({ quiet: summaryState === 'ready' });
    void startRefresh();
    return () => { summaryRequestId.current++; refreshRequestId.current++; };
  }, [active]);

  useEffect(() => {
    const pollingStatus = isRefreshRunning(postedRefreshStatus) ? postedRefreshStatus : refreshStatus(summary) || postedRefreshStatus;
    if (!active || (!refreshStarting && !isRefreshRunning(pollingStatus))) return;
    const timer = window.setInterval(() => { void loadSummary({ quiet: true }); }, 3000);
    return () => window.clearInterval(timer);
  }, [active, refreshStarting, summary, postedRefreshStatus]);

  const toggleFolder = (token: string) => {
    setExpanded(previous => {
      const next = new Set(previous);
      if (next.has(token)) next.delete(token);
      else next.add(token);
      return next;
    });
  };

  const renderFolder = (folder: CloudFolder) => {
    if (!visibleFolderTokens.has(folder.token)) return null;
    const childFolders = (folderChildren.get(folder.token) || []).filter(child => visibleFolderTokens.has(child.token));
    const childDocs = (documentChildren.get(folder.token) || []).filter(document => !query.trim() || matchedDocumentTokens.has(document.token));
    const isExpanded = query.trim() ? true : expanded.has(folder.token);
    const total = recursiveDocumentCount(folder, folderChildren, documentChildren);
    return <li key={folder.token} className="cloud-folder-item">
      <div className={`cloud-tree-row depth-${Math.min(folderDepth(folder), 4)}`}>
        <button type="button" className="cloud-tree-toggle" aria-label={`${isExpanded ? '收起' : '展开'}${folder.name}`} aria-expanded={isExpanded} onClick={() => toggleFolder(folder.token)}>
          <TreeChevron expanded={isExpanded} />
        </button>
        <button type="button" className="cloud-folder-button" onClick={() => toggleFolder(folder.token)}>
          <span className="cloud-folder-icon" aria-hidden="true" />
          <strong>{folder.name}</strong>
          <em>{total}</em>
        </button>
      </div>
      {isExpanded && <ul className="cloud-tree-children">
        {childFolders.map(renderFolder)}
        {childDocs.map(document => <li key={document.token}>
          <a className={`cloud-doc-row depth-${Math.min(folderDepth(folder) + 1, 5)}`} href={document.url} target="_blank" rel="noopener noreferrer" aria-label={`打开飞书清单：${document.name}`}>
            <span className="cloud-doc-mark" aria-hidden="true">SHT</span>
            <strong>{document.name}</strong>
            <span className="cloud-doc-open">打开清单 ↗</span>
          </a>
        </li>)}
      </ul>}
    </li>;
  };


  const currentRefresh = isRefreshRunning(postedRefreshStatus) ? postedRefreshStatus : refreshStatus(summary) || postedRefreshStatus;
  const runningRefresh = refreshStarting || isRefreshRunning(currentRefresh);
  const failedRefresh = refreshFailed(currentRefresh) || Boolean(refreshError);
  const statusClass = runningRefresh ? 'warning' : failedRefresh ? 'error' : 'success';

  return <section className="cloud-catalog" aria-labelledby="cloudCatalogTitle">
    <header className="cloud-catalog-hero">
      <div>
        <p className="eyebrow">FEISHU DMR CATALOG</p>
        <h2 id="cloudCatalogTitle">DMR 云清单</h2>
        <p>按飞书原目录分级，点击清单名称即可打开对应的飞书清单。</p>
        {summary && <p role="status">最后成功读取：{formatTime(lastSuccessTime(summary))}。进入页面、主动刷新或查找时更新目录；点击定位时重新读取对应清单。{(runningRefresh || failedRefresh) && refreshStatusText(summary, refreshError)}</p>}
      </div>
      <div className="cloud-catalog-actions">
        <span className={`status-badge ${statusClass}`}>{runningRefresh ? '正在同步' : failedRefresh ? '同步失败' : '目录已同步'}</span>
        <button type="button" className="secondary-button" disabled={runningRefresh} onClick={() => void startRefresh({ manual: true })}>{runningRefresh ? '同步中…' : '刷新目录'}</button>
        {summary && <a className="secondary-button" href={summary.root_url} target="_blank" rel="noopener noreferrer">打开飞书目录</a>}
      </div>
    </header>

    {summary && <div className="cloud-catalog-metrics" role="status">
      <div><strong id="cloudCatalogCategoryCount">{rootFolders.length}</strong><span>项目分类</span></div>
      <div><strong id="cloudCatalogProductCount">{productFolderCount}</strong><span>产品 / 目录</span></div>
      <div><strong id="cloudCatalogDocumentCount">{summary.documents.length}</strong><span>清单文档</span></div>
      <div><strong id="cloudCatalogFetchedAt">{formatTime(lastSuccessTime(summary))}</strong><span>{runningRefresh ? "正在同步飞书目录" : "最后成功读取时间"}</span></div>
      <div><strong id="cloudCatalogWritebackStatus">人工维护</strong><span>清单更新方式</span></div>
    </div>}

    {(summaryState === 'loading' || (!summary && runningRefresh)) && <div className="cloud-state"><strong>{runningRefresh ? '正在同步 DMR 云清单' : '正在读取 DMR 云清单'}</strong><span>{runningRefresh ? '首次同步可能需要一点时间，完成后会自动显示目录。' : '正在加载清单目录和链接。'}</span></div>}
    {summaryState === 'error' && !runningRefresh && <div className="cloud-state error"><strong>云清单读取失败</strong><span>{summaryError}</span><button type="button" className="row-button" onClick={() => void loadSummary()}>重试</button></div>}

    {summaryState === 'ready' && summary && <div className="cloud-catalog-shell">
      <aside className="cloud-catalog-tree" aria-label="DMR 云清单目录">
        <div className="cloud-tree-toolbar">
          <label className="cloud-search"><SearchIcon /><input value={query} onChange={event => setQuery(event.currentTarget.value)} type="search" placeholder="搜索清单、产品、主体" aria-label="搜索 DMR 云清单" /></label>
          <div>
            <button type="button" className="text-button" onClick={() => setExpanded(new Set(summary.folders.map(folder => folder.token)))}>全部展开</button>
            <button type="button" className="text-button" onClick={() => setExpanded(new Set(rootFolders.map(folder => folder.token)))}>收起产品</button>
          </div>
        </div>
        <ul className="cloud-tree-root">
          {rootFolders.map(renderFolder)}
        </ul>
        {!summary.documents.length && <div className="cloud-tree-empty">云清单目录暂无文档。</div>}
        {summary.documents.length > 0 && query.trim() && matchedDocumentTokens.size === 0 && <div className="cloud-tree-empty">没有匹配的清单。</div>}
      </aside>


    </div>}
  </section>;
}
