import { useEffect, useRef, useState } from 'react';
import { api, apiErrorMessage } from '../api';

export type CatalogSearch = { registrant: string; projectCategory: string; product: string; module: string; query: string; revision: number };
type Result = { documents: Array<{ token: string; name: string; url: string; path: string[]; catalog_match_unverified?: boolean; matches?: Array<{name?: string; code?: string; value?: string; row?: number; sheet_name?: string}> }>; fetched_at: string };
export function CatalogSearchResults({ search, onClear }: { search: CatalogSearch; onClear: () => void }) {
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState('');
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    let cancelled = false;
    setResult(null); setError('');
    panel.current?.scrollIntoView({ block: 'start' });
    const params = new URLSearchParams({ query: search.query, registrant: search.registrant, project_category: search.projectCategory, product: search.product, module: search.module });
    api<Result>(`/api/dmr-catalog-search?${params}`).then(value => { if (!cancelled) setResult(value); }).catch(reason => { if (!cancelled) setError(apiErrorMessage(reason)); });
    return () => { cancelled = true; };
  }, [search]);
  return <section ref={panel} className="panel" aria-label="云清单搜索结果" style={{ padding: 24, marginBottom: 24, scrollMarginTop: 90 }}>
    <div className="panel-head"><h2>对应 DMR 云清单{result ? `（${result.documents.length}）` : ''}</h2><button className="text-button" onClick={onClear}>清除查询</button></div>
    <p>{[search.registrant, search.projectCategory, search.product, search.module, search.query].filter(value => value && value !== 'all').join(' / ') || '全部清单'}</p>
    <p className="cell-sub">按已读取的云清单内容查询。打开原清单查看最新内容；未命中不代表云端不存在。</p>
    <div role="status">{error || (!result ? '正在查找云清单…' : !result.documents.length ? '未找到符合条件的清单，请减少关键词或放宽筛选，也可在下方云清单目录中查找。' : '')}</div>
    {result && <div style={{ maxHeight: 440, overflowY: 'auto' }}>{result.documents.map(doc => <a key={doc.token} className="cloud-doc-row" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16 }} href={doc.url} target="_blank" rel="noopener noreferrer"><span style={{ minWidth: 0, flex: 1, overflowWrap: 'anywhere' }}><strong>{doc.name}</strong><small className="cell-sub">{doc.path.join(' / ')}</small>{doc.catalog_match_unverified && <small className="cell-sub">已找到清单，内容尚未读取，请打开核对模块。</small>}{doc.matches?.slice(0, 3).map((match, index) => <small className="cell-sub" key={index}>{[match.sheet_name, match.row ? `第 ${match.row} 行` : '', match.code, match.name || match.value].filter(Boolean).join(' · ')}</small>)}</span><span className="cloud-doc-open">打开清单 ↗</span></a>)}</div>}
  </section>;
}
