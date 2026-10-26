import { useEffect, useRef, useState } from 'react';
import { ApiError, api, apiErrorMessage } from '../api';
import { modules } from './shared';
import './CatalogUpdates.css';

type MatchBasis = 'code' | 'name' | 'normalized_name' | 'name_changed_code' | 'keyword_name';
type CatalogLocation = { sheet_name: string; row: number; code_cell?: string; name_cell?: string; version_column: string; version_cell: string; snapshot_version: string; read_at: string };
type LiveLocationMatch = {
  sheet_name: string; sheet_id?: string; row: number; code_cell?: string; name_cell?: string; version_column?: string; version_cell: string;
  current_version?: string; read_at: string; region_title?: string; region_area?: string; header_row?: number;
  region_start_column?: string; region_end_column?: string; version_comparison?: number | null;
  file_name?: string; current_code?: string; module?: string; match_basis?: MatchBasis;
};
type LiveLocationResult = {
  unresolved_hits?: {sheet_name:string;code_cell?:string;name_cell?:string}[];
  code_hits?: {sheet_name:string;sheet_id?:string;row?:number;code_cell:string;read_at:string}[];
  version_comparison?: number | null; proposed_version?: string; status: string; message?: string;
  document_name?: string; document_url?: string; code?: string; file_name?: string; module?: string; matches?: LiveLocationMatch[]; location?: LiveLocationMatch;
};
type StoredLiveLocationResult = LiveLocationResult & { located_at?: number };
type CatalogDocument = { locations?: Record<string, CatalogLocation>; token: string; name: string; url: string; path: string[] };
type ScanResult = { document: CatalogDocument; result: LiveLocationResult };
type CatalogScan = { revision: number; running: boolean; total: number; results: ScanResult[]; stopped?: boolean; error?: string };
type CatalogTarget = { document_token: string; document_name: string; document_url: string; status: 'pending' | 'processed'; processed_by?: string; processed_at?: string; note?: string };
type ManagementEvidence = { label?: string; field?: string; value: string; source?: string };
type CatalogUpdate = {
  management_target?: string; management_target_label?: string; management_instruction?: string;
  direct_catalog_row?: boolean; catalog_locator_enabled?: boolean; catalog_location_hint?: string;
  update_kind?: string; relation_evidence?: ManagementEvidence[]; relation_candidates?: {code: string; basis?: string; product?: string; registrant?: string; row?: string; matched_value?: string}[];
  targets?: CatalogTarget[]; target_count?: number; processed_count?: number;
  demo?: boolean; id: string; revision: number; status: 'pending' | 'processed';
  record_id: string; file_id: string; file_name: string; code: string;
  registrant: string; product: string; module: string; old_version: string; new_version: string;
  created_at: string; updated_at: string; document_token?: string; document_name?: string; document_url?: string;
  match_status: 'matched' | 'unmatched' | 'ambiguous'; candidate_tokens: string[]; shared_candidate_tokens?: string[];
  processed_by?: string; processed_at?: string; note?: string; download_url: string;
};
type UpdatesResponse = { items: CatalogUpdate[]; can_manage: boolean; catalog_documents: CatalogDocument[]; catalog_error?: string };
type LocateOptions = { fileName?: string; module?: string; version?: string };

function timeText(value?: string) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

export function CatalogUpdates({ active }: { active: boolean }) {
  const [scans, setScans] = useState<Record<string, CatalogScan>>({});
  const scanRuns = useRef<Record<string, number>>({});
  useEffect(() => () => { for (const key of Object.keys(scanRuns.current)) scanRuns.current[key]++; }, []);
  const [associating, setAssociating] = useState<string | null>(null);
  const [associationTokens, setAssociationTokens] = useState<string[]>([]);
  const [associationNote, setAssociationNote] = useState('');
  const [associationRevision, setAssociationRevision] = useState(0);
  const [associationSearch, setAssociationSearch] = useState('');
  const [liveLocations, setLiveLocations] = useState<Record<string, StoredLiveLocationResult>>({});
  const [locationClock, setLocationClock] = useState(() => Date.now());
  const [locating, setLocating] = useState<Record<string, boolean>>({});
  const inFlight = useRef(new Set<string>());
  const [testCode, setTestCode] = useState('');
  const [testFileName, setTestFileName] = useState('');
  const [testModule, setTestModule] = useState('');
  const [testDocument, setTestDocument] = useState('');
  const [testVersion, setTestVersion] = useState('');
  const [showDemo, setShowDemo] = useState(() => new URLSearchParams(window.location.search).get('demoUpdate') === '1');
  const [demoDone, setDemoDone] = useState(false);
  const [data, setData] = useState<UpdatesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [editing, setEditing] = useState<string | null>(null);
  const [editingRevision, setEditingRevision] = useState(0);
  const [note, setNote] = useState('');
  const [documentToken, setDocumentToken] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const request = useRef(0);

  useEffect(() => {
    if (!active) {
      setLiveLocations({});
      return;
    }
    const timer = window.setInterval(() => setLocationClock(Date.now()), 15000);
    return () => window.clearInterval(timer);
  }, [active]);

  useEffect(() => {
    if (!active) return;
    const onCatalogRefreshed = () => setReload(value => value + 1);
    window.addEventListener('dmr-catalog-refreshed', onCatalogRefreshed);
    return () => window.removeEventListener('dmr-catalog-refreshed', onCatalogRefreshed);
  }, [active]);

  useEffect(() => {
    if (!active) return;
    const current = ++request.current;
    setLoading(true);
    setError('');
    api<UpdatesResponse>('/api/catalog-updates').then(result => {
      if (current === request.current) setData(result);
    }).catch(reason => {
      if (current === request.current) {
        setData(null);
        setError(apiErrorMessage(reason));
      }
    }).finally(() => { if (current === request.current) setLoading(false); });
    return () => { request.current++; };
  }, [active, reload]);

  const demoDocument = data?.catalog_documents[0];
  const demoItem: CatalogUpdate | null = showDemo && demoDocument ? { demo: true, id: 'DEMO-UPDATE', revision: 1, status: demoDone ? 'processed' : 'pending', record_id: '', file_id: '', file_name: '【演示】11UI04AA01_UI04（A1）成品BOM_20260924.xlsx', code: '11UI04AA01', registrant: '广西由莱', product: 'UI04（A1）', module: 'BOM', old_version: '20260410', new_version: '20260924', created_at: '', updated_at: '', document_token: demoDocument.token, document_name: demoDocument.name, document_url: demoDocument.url, match_status: 'matched', candidate_tokens: [], download_url: '', processed_by: '演示操作', note: '仅模拟页面效果，未保存处理记录。' } : null;
  const pending = data?.items.filter(item => item.status === 'pending') || [];
  const processed = data?.items.filter(item => item.status === 'processed') || [];
  const selectedDocument = data?.catalog_documents.find(document => document.token === documentToken);

  function begin(item: CatalogUpdate) {
    setEditing(item.id + ':' + (item.document_token || ''));
    setEditingRevision(item.revision);
    setNote('');
    setDocumentToken(item.document_token || '');
    setConfirmed(false);
    setError('');
    setNotice('');
  }

  async function resolve(item: CatalogUpdate) {
    const requiresNote = item.match_status !== 'matched' || documentToken !== item.document_token;
    if (busy || !confirmed || !documentToken || (requiresNote && !note.trim())) return;
    setBusy(true);
    setError('');
    try {
      await api(`/api/catalog-updates/${encodeURIComponent(item.id)}/resolve`, {
        revision: editingRevision, note: note.trim(), document_token: documentToken,
      });
      setEditing(null);
      setNotice('已记录此份清单的处理结果；其他清单仍需逐份核对。此操作没有修改飞书。');
      setReload(value => value + 1);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        setEditing(null);
        setNotice('这条提醒已发生变化，正在刷新。请按最新版本重新核对。');
        setReload(value => value + 1);
      } else setError(apiErrorMessage(reason));
    } finally { setBusy(false); }
  }

  async function locate(key: string, token: string, code: string, item?: CatalogUpdate, options: LocateOptions = {}) {
    const cleanCode = code.trim();
    const cleanFileName = (options.fileName || item?.file_name || '').trim();
    const cleanModule = (options.module || item?.module || '').trim();
    if (!token || (!cleanCode && !cleanFileName && !item) || inFlight.current.has(key)) return;
    inFlight.current.add(key);
    setLocating(value => ({ ...value, [key]: true }));
    setLiveLocations(value => ({ ...value, [key]: { status: 'reading' } }));
    try {
      const result = await api<LiveLocationResult>('/api/catalog-locate', {
        document_token: token,
        code: cleanCode,
        filename: cleanFileName,
        module: cleanModule,
        proposed_version: item?.new_version || options.version || (key === 'test' ? testVersion : ''),
        ...(item && !item.demo ? {notice_id: item.id, revision: item.revision} : {}),
      });
      setLiveLocations(value => ({ ...value, [key]: {...result, located_at: Date.now()} }));
    } catch (reason) {
      setLiveLocations(value => ({ ...value, [key]: {status: 'error', message: apiErrorMessage(reason)} }));
    } finally {
      inFlight.current.delete(key);
      setLocating(value => ({ ...value, [key]: false }));
    }
  }

  function locationPanel(key: string, newVersion = '') {
    const result = liveLocations[key];
    if (locating[key]) return <p role="status">正在只读检查飞书清单，请稍候…</p>;
    if (!result) return null;
    const matches = visibleMatches(result);
    if (result.status === 'not_found') return <div className="catalog-update-location" role="status"><strong>已找到对应清单 · 未定位到现有条目</strong><p>{result.message}</p><p>平台会保留这份清单链接。请先打开清单核对旧名称和旧编号；确认确实没有后，再由文控手动新增或修改，避免重复记录。</p>{result.document_url && <a href={result.document_url} target="_blank" rel="noopener noreferrer">打开对应清单核对 ↗</a>}</div>;
    if (result.status !== 'located' && !(result.status === 'ambiguous' && matches.length)) return <p className="catalog-updates-error" role="alert">{result.message || '未找到唯一可靠位置，请按编号或文件名人工核对。'}</p>;
    const expired = result.located_at ? locationClock - result.located_at > 60000 : false;
    return <div className={`catalog-update-location ${expired ? 'expired' : ''}`} role="status">
      <strong>{expired ? '定位已超过 60 秒，请重新读取' : result.status === 'ambiguous' ? (matches.length > 1 ? `找到多处条目 · ${matches.length} 个位置待确认` : '找到名称候选 · 待人工核对') : `找到唯一条目：${matches[0].version_cell} · 第 ${matches[0].row} 行`}</strong>
      {!expired && <><p>清单：{result.document_name || '当前清单'}</p>
      {result.status === 'ambiguous' && <p className="catalog-update-hint">{result.message || '同一编号存在多个有效位置。'} 请逐项核对区域、名称及版本，决定哪些位置需要更新；不会默认只取第一处或全部更新。</p>}
      {!!result.unresolved_hits?.length && <p className="catalog-updates-error">另有位置未能确定版本字段：{result.unresolved_hits.map(hit => `${hit.sheet_name} ${hit.code_cell || hit.name_cell || '待确认单元格'}`).join('；')}，请人工核对。</p>}
      <div className="catalog-location-matches">{matches.map((match, index) => renderLocationMatch(result, match, newVersion, matches.length > 1 ? index + 1 : undefined))}</div>
      <small>读取时间：{timeText(matches[0]?.read_at || result.location?.read_at)}。位置按文件编号或文件名称实时查找；修改前请核对命中项，期间插行后可重新定位。</small></>}
      {expired && <p>清单可能已被他人编辑，行号不再作为当前核实结果。请点击“重新读取并定位”后再修改。</p>}
      {result.document_url && <p><a href={result.document_url} target="_blank" rel="noopener noreferrer">打开对应清单 ↗</a></p>}
    </div>;
  }

  function visibleMatches(result: LiveLocationResult) {
    return (result.location ? [result.location] : result.matches || []).filter(match => match && (match.code_cell || match.name_cell) && match.version_cell);
  }

  function renderLocationMatch(result: LiveLocationResult, match: LiveLocationMatch, newVersion = '', index?: number) {
    const comparison = match.version_comparison ?? result.version_comparison;
    const region = formatRegion(match);
    const range = match.region_start_column && match.region_end_column ? ` · 区域列 ${match.region_start_column}-${match.region_end_column}` : '';
    const reference = matchReference(match, result);
    return <article className="catalog-location-match" key={`${match.sheet_id || match.sheet_name}:${match.code_cell || match.name_cell}:${match.version_cell}:${index || 0}`}>
      <strong>{index ? `候选 ${index}：` : ''}{region} · {match.sheet_name}</strong>
      <p>{reference.label}：{reference.cell}{reference.value ? ` = ${reference.value}` : ''}；版本单元格：{match.version_cell}；第 {match.row} 行{match.header_row ? `；表头第 ${match.header_row} 行` : ''}{range}</p>
      {(match.module || result.module) && <p>文件类型：{match.module || result.module}</p>}
      <p>云端读取值：<strong>{match.current_version || '空白'}</strong>{newVersion && <>；本次文件版本：<strong>{newVersion}</strong></>}</p>
      {newVersion && result.proposed_version === newVersion && comparison != null && comparison < 0 && <p className="catalog-updates-error"><strong>云端版本较新，请勿降版覆盖。</strong>请核对本次上传文件是否为旧版。</p>}
      {newVersion && match.current_version === newVersion && <p>清单版本与本次文件一致，仍请核对文件名称、编号和修订内容是否已按实际更新。</p>}
    </article>;
  }

  function formatRegion(match: LiveLocationMatch) {
    return [match.region_title || '未命名区域', match.region_area].filter(Boolean).join(' / ');
  }

  function matchReference(match: LiveLocationMatch, result: LiveLocationResult) {
    const basis = match.match_basis || (match.code_cell ? 'code' : 'name');
    if (basis === 'code') return { label: '编号命中单元格', cell: match.code_cell || match.name_cell || '待确认', value: result.code || match.file_name || '' };
    if (basis === 'name_changed_code') return {
      label: '文件名称候选单元格（编号可能变更，请人工确认）',
      cell: match.name_cell || match.code_cell || '待确认',
      value: `${match.file_name || result.file_name || ''}${match.current_code ? `；清单原编号：${match.current_code}` : ''}`,
    };
    if (basis === 'keyword_name') return {
      label: '文件名称关键词候选单元格（需人工确认）',
      cell: match.name_cell || match.code_cell || '待确认',
      value: match.file_name || result.file_name || '',
    };
    return {
      label: basis === 'normalized_name' ? '文件名称候选单元格（需人工确认）' : '文件名称命中单元格',
      cell: match.name_cell || match.code_cell || '待确认',
      value: match.file_name || result.file_name || '',
    };
  }

  function matchSummary(result: LiveLocationResult, match?: LiveLocationMatch) {
    const basis = match?.match_basis || (match?.code_cell || result.code_hits?.length ? 'code' : 'name');
    if (basis === 'code') return '文件编号命中';
    if (basis === 'normalized_name') return '文件名称候选命中，需人工确认';
    if (basis === 'name_changed_code') return '文件名称候选命中，编号可能变更';
    if (basis === 'keyword_name') return '文件名称关键词命中，需人工确认';
    return '文件名称命中';
  }

  async function scanCatalogs(item: CatalogUpdate) {
    if (!data || Object.values(scans).some(scan => scan.running)) return;
    const generation = (scanRuns.current[item.id] || 0) + 1;
    scanRuns.current[item.id] = generation;
    setScans(values => ({...values, [item.id]: {revision: item.revision, running: true, total: 0, results: []}}));
    let refreshed: UpdatesResponse;
    try {
      refreshed = await api<UpdatesResponse>('/api/catalog-updates/refresh-list', {});
    } catch (reason) {
      if (scanRuns.current[item.id] !== generation) return;
      setScans(values => ({...values, [item.id]: {revision: item.revision, running: false, total: 0, results: [], stopped: true, error: apiErrorMessage(reason)}}));
      return;
    }
    if (scanRuns.current[item.id] !== generation) return;
    setData(refreshed);
    const latestItem = refreshed.items.find(entry => entry.id === item.id) || item;
    const documents = [...refreshed.catalog_documents];
    let cursor = 0;
    let stale = false;
    setScans(values => ({...values, [item.id]: {revision: latestItem.revision, running: true, total: documents.length, results: []}}));
    const worker = async () => {
      while (scanRuns.current[item.id] === generation && !stale && cursor < documents.length) {
        const document = documents[cursor++];
        let result: LiveLocationResult;
        try {
          result = await api<LiveLocationResult>('/api/catalog-locate', {document_token: document.token, code: latestItem.code, filename: latestItem.file_name, module: latestItem.module, proposed_version: latestItem.new_version, ...(latestItem.status === 'pending' ? {notice_id:latestItem.id, revision:latestItem.revision} : {})});
        } catch (reason) {
          result = {status:'read_failed',message:apiErrorMessage(reason)};
          if (reason instanceof ApiError && [401,403,409].includes(reason.status)) stale = true;
        }
        if (scanRuns.current[item.id] !== generation) return;
        setScans(values => ({...values, [item.id]: {...values[item.id], results:[...values[item.id].results,{document,result}]}}));
      }
    };
    await Promise.all([worker(), worker()]);
    if (scanRuns.current[item.id] === generation) setScans(values => ({...values,[item.id]:{...values[item.id],running:false,stopped:stale}}));
  }

  function stopScan(item: CatalogUpdate) {
    scanRuns.current[item.id] = (scanRuns.current[item.id] || 0) + 1;
    setScans(values => ({...values,[item.id]:{...values[item.id],running:false,stopped:true}}));
  }

  function scanPanel(item: CatalogUpdate, locked: string[]) {
    const scan = scans[item.id];
    const current = scan?.revision === item.revision ? scan : undefined;
    const matched = current?.results.filter(entry => entry.result.status === 'located' || (entry.result.status === 'ambiguous' && Boolean(entry.result.code_hits?.length || visibleMatches(entry.result).length))) || [];
    const failed = current?.results.filter(entry => entry.result.status === 'read_failed' || (entry.result.status !== 'located' && entry.result.status !== 'not_found' && !visibleMatches(entry.result).length && !entry.result.code_hits?.length)) || [];
    const rank = (doc: CatalogDocument) => {
      const context = doc.path.join(' / ').toUpperCase();
      const codes = item.product.toUpperCase().match(/[A-Z]+[0-9]+[A-Z0-9]*/g) || [];
      return (item.registrant && context.includes(item.registrant.toUpperCase()) ? 2 : 0) + (codes.length && codes.every(code => new RegExp('(^|[^A-Z0-9])'+code+'([^A-Z0-9]|$)').test(context)) ? 1 : 0);
    };
    matched.sort((a,b) => rank(b.document)-rank(a.document) || a.document.name.localeCompare(b.document.name));
    return <section className="catalog-scan-panel" aria-label="全局扫描推荐">
      <div className="catalog-update-actions"><button type="button" className="secondary-button" disabled={busy || Object.values(scans).some(value=>value.running)} onClick={() => void scanCatalogs(item)}>{current ? '重新扫描全部清单' : '扫描全部清单并推荐'}</button>{current?.running && <button type="button" className="secondary-button" onClick={()=>stopScan(item)}>停止扫描</button>}</div>
      <p>范围为平台已关联目录中的 {data?.catalog_documents.length || 0} 份清单；按文件编号或文件名称，以及文件类型读取最新内容，不限主体或产品。候选需人工勾选确认，不会自动关联。</p>
      {current && <><p role="status">{current.error ? `扫描前刷新清单失败：${current.error}` : current.total === 0 && current.running ? '正在刷新云清单列表，成功后再开始扫描。' : `${current.running ? '扫描中' : current.stopped ? '扫描已停止' : '本轮扫描结束'}：已返回 ${current.results.length}/${current.total} 份 · 候选 ${matched.length} 份 · 未可靠检查 ${failed.length + current.total-current.results.length} 份`}</p>
      <p>分批读取，各清单以各自读取时间为准；未匹配不等于没有权限或读取失败。</p>
      {matched.map(({document,result}) => {
        const matches = visibleMatches(result);
        return <label className="catalog-scan-candidate" key={document.token}><input type="checkbox" checked={associationTokens.includes(document.token)} disabled={busy || locked.includes(document.token)} onChange={event=>setAssociationTokens(values=>event.target.checked ? [...new Set([...values,document.token])] : values.filter(token=>token!==document.token))}/><span><strong>{document.name}</strong><br/>{rank(document) === 3 ? `${matchSummary(result, matches[0])}，主体及产品匹配` : `${matchSummary(result, matches[0])}，可能跨主体或跨产品共用，请确认`}<br/>{matches.length ? <>{matches.length > 1 && <><strong>需人工确认适用区域，以下仅为候选位置。</strong><br/></>}{matches.map((match,index) => { const ref = matchReference(match, result); return <span className="catalog-scan-match" key={`${match.sheet_id || match.sheet_name}:${match.code_cell || match.name_cell}:${match.version_cell}:${index}`}>{matches.length > 1 ? `候选 ${index + 1}：` : ''}{formatRegion(match)} · {match.sheet_name} · {ref.label} {ref.cell} · 版本 {match.version_cell}：{match.current_version || '空白'}{match.module ? ` · ${match.module}` : ''}{match.region_start_column && match.region_end_column ? ` · 区域列 ${match.region_start_column}-${match.region_end_column}` : ''}{match.header_row ? ` · 表头第 ${match.header_row} 行` : ''}{(match.version_comparison ?? result.version_comparison) != null && (match.version_comparison ?? result.version_comparison)! < 0 ? ' · 清单版本较新，请勿降版覆盖' : ''}</span>; })}</> : `${matchSummary(result)}：${result.code_hits?.map(hit=>hit.sheet_name+' '+hit.code_cell).join('；') || '多处'}；版本字段尚需人工核对`}{result.version_comparison != null && result.version_comparison < 0 && !matches.length && <><br/><strong>清单版本较新，请勿降版覆盖。</strong></>}<br/><small>勾选只确认适用清单，区域仍需人工打开清单核对。</small><br/><a href={document.url} target="_blank" rel="noopener noreferrer">打开清单 ↗</a></span></label>;
      })}
      {!current.error && !current.running && !matched.length && <p>本轮没有找到候选，请检查下方未检查项目，或从全部清单中人工选择。</p>}
      {failed.length > 0 && <details><summary>未可靠检查 / 需人工核对 · {failed.length} 份</summary>{failed.map(({document,result})=><p key={document.token}><a href={document.url} target="_blank" rel="noopener noreferrer">{document.name}</a>：{result.message || '读取未完成'}</p>)}</details>}
      {current.total > current.results.length && <details><summary>尚未完成检查 · {current.total-current.results.length} 份</summary>{data?.catalog_documents.filter(doc=>!current.results.some(entry=>entry.document.token===doc.token)).map(doc=><p key={doc.token}>{doc.name}</p>)}</details>}
      </>}
    </section>;
  }

  async function saveAssociations(item: CatalogUpdate) {
    if (busy || !associationTokens.length || !associationNote.trim()) return;
    setBusy(true); setError('');
    try {
      await api(`/api/catalog-updates/${encodeURIComponent(item.id)}/associations`, {revision: associationRevision, document_tokens: associationTokens, note: associationNote.trim()});
      setAssociating(null); setEditing(null); setLiveLocations({});
      setNotice('已确认关联清单；后续升版沿用此关系，各份清单分别处理。');
      setReload(value => value + 1);
    } catch (reason) {
      setError(apiErrorMessage(reason));
      if (reason instanceof ApiError && reason.status === 409) { setAssociating(null); setReload(value => value + 1); }
    } finally { setBusy(false); }
  }

  function managementPanel(item: CatalogUpdate) {
    if (!item.management_instruction) return null;
    return <section className="catalog-update-location" aria-label="本次核对内容">
      <strong>处理对象：{item.management_target_label || 'DMR 清单'}</strong>
      <p>{item.management_instruction}</p>
      {item.catalog_location_hint && <p>{item.catalog_location_hint}</p>}
      {!!item.relation_candidates?.length && <details><summary>查看关联 BOM 候选 · {item.relation_candidates.length} 条</summary>
        {item.relation_candidates.map((entry, index) => <p key={index}>{entry.code} · {[entry.registrant, entry.product].filter(Boolean).join(' / ')}{entry.basis === 'bom_component' ? ` · BOM 第 ${entry.row} 行命中 ${entry.matched_value}` : ' · 来自已解析的关联信息'}</p>)}
        <p>仅包含已解析且有权查看的资料；需人工确认适用范围，不代表全部产品。</p>
      </details>}
      {!!item.relation_evidence?.length && <details><summary>查看关联依据 · {item.relation_evidence.length} 条</summary>
        {item.relation_evidence.map((entry, index) => <p key={index}>{entry.label || entry.field}：{entry.value}</p>)}
      </details>}
    </section>;
  }

  async function resolveManagement(item: CatalogUpdate) {
    if (busy || !confirmed || !note.trim()) return;
    setBusy(true); setError('');
    try {
      await api(`/api/catalog-updates/${encodeURIComponent(item.id)}/resolve`, {revision: editingRevision, note: note.trim()});
      setEditing(null); setNotice('已保存人工核对记录；平台未修改外部台账或飞书内容。');
      setReload(value => value + 1);
    } catch (reason) {
      setError(apiErrorMessage(reason));
      if (reason instanceof ApiError && reason.status === 409) { setEditing(null); setReload(value => value + 1); }
    } finally { setBusy(false); }
  }

  function renderManagement(item: CatalogUpdate) {
    const done = item.status === 'processed';
    const isEditing = editing === 'management:' + item.id;
    const candidates = [...new Set([item.document_token, ...(item.candidate_tokens || [])])]
      .map(token => data?.catalog_documents.find(doc => doc.token === token)).filter((doc): doc is CatalogDocument => !!doc);
    const relatedDocument = item.document_url ? { name: item.document_name || '相关产品清单', url: item.document_url } : candidates[0] ? { name: candidates[0].name, url: candidates[0].url } : null;
    return <details className={`catalog-update-card ${done ? 'processed' : ''}`} key={item.id}>
      <summary className="catalog-update-summary"><span className="catalog-update-summary-main">
        <span className={`catalog-update-state ${done ? 'done' : ''}`}>{done ? '文控已确认' : relatedDocument ? '已找到相关清单 · 未定位具体条目' : '待确认适用清单'}</span>
        <strong>{item.management_target_label || '配套资料核对'}</strong>
        <span className="catalog-update-summary-meta">{item.code} · {item.new_version || '版本未提供'}</span>
      </span><span className="catalog-update-fold-label"><span className="when-closed">展开详情</span><span className="when-open">收起详情</span></span></summary>
      <div className="catalog-update-body">
        <p className="catalog-update-context">{[item.registrant, item.product, item.module].filter(Boolean).join(' / ')} · {timeText(item.created_at)}</p>
        <p className="catalog-update-filename">{item.file_name}</p>
        {relatedDocument && <p className="catalog-update-related-document">相关清单：<a href={relatedDocument.url} target="_blank" rel="noopener noreferrer">{relatedDocument.name} ↗</a><span>尚未定位到此文件的具体条目；请文控打开清单核对，并按实际情况手动处理。</span></p>}
        {managementPanel(item)}
        {!!candidates.length && <details><summary>相关产品清单入口 · {candidates.length} 份</summary><p>以下链接用于核对产品资料，不代表已定位本文件的专用台账。</p>
          {candidates.map(doc => <p key={doc.token}><a href={doc.url} target="_blank" rel="noopener noreferrer">{doc.name} ↗</a></p>)}
        </details>}
        <div className="catalog-update-actions">
          {item.download_url && <a className="secondary-button" href={item.download_url} download>下载本次文件</a>}
          {!done && data?.can_manage && <button className="secondary-button" disabled={busy} onClick={() => {setEditing('management:' + item.id); setEditingRevision(item.revision); setNote(''); setConfirmed(false);}}>记录人工处理结果</button>}
        </div>
        {done && <p className="catalog-update-receipt">{item.processed_by || '文控'} · {timeText(item.processed_at)} · {item.note}<br/>仅记录人工确认，未核验外部资料。</p>}
        {isEditing && <form className="catalog-update-form" onSubmit={event => {event.preventDefault(); void resolveManagement(item);}}>
          <label htmlFor={'management-note-' + item.id}>处理位置和结果（必填）</label>
          <textarea id={'management-note-' + item.id} required maxLength={2000} rows={3} disabled={busy} value={note} onChange={event => setNote(event.target.value)} placeholder="请说明核对的台账/配套资料名称、位置，以及已修改的内容或无需修改的原因"/>
          <label className="catalog-update-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)}/><span>我已核对相应管理记录并完成必要处理，确认上述说明准确。</span></label>
          <div className="catalog-update-actions"><button className="primary-button" disabled={busy || !confirmed || !note.trim()}>{busy ? '保存中…' : '保存处理记录'}</button><button className="secondary-button" type="button" disabled={busy} onClick={() => setEditing(null)}>取消</button></div>
        </form>}
      </div>
    </details>;
  }

  function renderGroup(item: CatalogUpdate) {
    if (item.direct_catalog_row === false) return renderManagement(item);
    const targets = item.targets || [];
    const doneCount = targets.filter(target => target.status === 'processed').length;
    const locked = targets.filter(target => target.status === 'processed').map(target => target.document_token);
    return <section className="catalog-update-group" key={item.id} aria-label={`文件 ${item.code} 的关联清单`}>
      <div className="catalog-group-heading"><strong>{item.code} · {item.new_version}</strong><span>{targets.length || item.document_token ? `文控已确认 ${doneCount}/${targets.length || 1} 份清单` : '待确认适用清单'}</span>
      {!item.demo && data?.can_manage && <button className="secondary-button" disabled={busy} onClick={() => {setAssociating(item.id); setAssociationRevision(item.revision); setAssociationTokens(targets.length ? targets.map(target => target.document_token) : item.document_token ? [item.document_token] : []); setAssociationNote(''); setAssociationSearch(''); setEditing(null); if (!scans[item.id] || scans[item.id].revision !== item.revision) void scanCatalogs(item);}}>管理关联清单</button>}</div>

      {associating === item.id && <form className="catalog-update-form" onSubmit={event => {event.preventDefault(); void saveAssociations(item);}}>
        <p>可关联多份清单，包含跨主体或跨产品共用。保存关联不代表已修改清单。文控已确认的清单保留记录，不能取消。</p>
        {scanPanel(item, locked)}
        <label htmlFor={`association-search-${item.id}`}>从全部清单中查找</label><input id={`association-search-${item.id}`} value={associationSearch} onChange={event => setAssociationSearch(event.target.value)} placeholder="输入主体、产品或清单名称" />
        <fieldset className="catalog-association-options"><legend>选择适用清单 · 已选 {associationTokens.length} 份</legend>{data?.catalog_documents.filter(doc => doc.path.join(' / ').toLowerCase().includes(associationSearch.trim().toLowerCase())).map(doc => <label key={doc.token}><input type="checkbox" checked={associationTokens.includes(doc.token)} disabled={busy || locked.includes(doc.token)} onChange={event => setAssociationTokens(values => event.target.checked ? [...values, doc.token] : values.filter(token => token !== doc.token))}/><span>{doc.path.join(' / ')}{locked.includes(doc.token) ? '（文控已确认）' : ''}</span></label>)}</fieldset>
        <label htmlFor={`association-note-${item.id}`}>关联确认说明（必填）</label><textarea id={`association-note-${item.id}`} required maxLength={2000} value={associationNote} onChange={event => setAssociationNote(event.target.value)} placeholder="例如：该文件由这两个产品共用，已确认均需核对清单" />
      <div className="catalog-update-actions"><button className="primary-button" disabled={busy || !associationTokens.length || !associationNote.trim()}>保存关联清单</button><button type="button" className="secondary-button" disabled={busy} onClick={() => setAssociating(null)}>取消</button></div>
      </form>}
      {targets.length ? targets.map(target => renderItem({...item, ...target, match_status: 'matched'})) : renderItem(item)}
      {(item.candidate_tokens || []).some(token => !targets.some(target => target.document_token === token) && token !== item.document_token) && <details className="catalog-scan-panel" open><summary>其他候选清单 · 请确认是否共用</summary><p>同主体优先展示；同型号或已读取内容中编号、名称命中的其他清单也会列出。请读取最新位置，再通过“管理关联清单”确认全部适用清单。</p>{(item.candidate_tokens || []).map(token => data?.catalog_documents.find(doc => doc.token === token)).filter((doc): doc is CatalogDocument => !!doc && !targets.some(target => target.document_token === doc.token) && doc.token !== item.document_token).map(doc => <p key={doc.token}><strong>{item.shared_candidate_tokens?.includes(doc.token) ? '可能跨主体或跨型号共用' : '同型号候选'}</strong> · <a href={doc.url} target="_blank" rel="noopener noreferrer">{doc.name} ↗</a></p>)}</details>}
    </section>;
  }

  function renderItem(item: CatalogUpdate) {
    const done = item.status === 'processed';
    const locationDocument = data?.catalog_documents.find(doc => doc.token === (editing === item.id + ':' + (item.document_token || '') ? documentToken : item.document_token));
    const snapshotLocation = locationDocument?.locations?.[item.code];
    const locationKey = item.id + ':' + item.revision + ':' + (locationDocument?.token || '');
    const locatedResult = liveLocations[locationKey];
    const locationExpired = locatedResult?.located_at ? locationClock - locatedResult.located_at > 60000 : false;
    const isEditing = editing === item.id + ':' + (item.document_token || '');
    const requiresNote = item.match_status !== 'matched' || documentToken !== item.document_token;
    return <details className={`catalog-update-card ${done ? 'processed' : ''}`} key={item.id + ':' + (item.document_token || '')}>
      <summary className="catalog-update-summary">
        <span className="catalog-update-summary-main"><span className={`catalog-update-state ${done ? 'done' : ''}`}>{item.demo ? '演示 · ' : ''}{done ? '文控已确认' : item.match_status === 'matched' ? '待核对条目' : '待确认清单'}</span><strong>{item.document_name || `${item.registrant || '主体待确认'} · ${item.product || '产品待确认'}`}</strong><span className="catalog-update-summary-meta">{item.code} · {item.old_version || '首次归档'} → {item.new_version}</span></span>
        <span className="catalog-update-fold-label"><span className="when-closed">展开详情</span><span className="when-open">收起详情</span></span>
      </summary>
      <div className="catalog-update-body">
      {item.demo && <p className="catalog-update-hint"><strong>演示提示 · 模拟数据</strong> — 仅查看效果，不代表真实文件更新，不会写入归档或飞书。</p>}
      <p className="catalog-update-context">{[item.registrant, item.product, item.module].filter(Boolean).join(' / ')} · 编号 {item.code || '待确认'}{item.created_at ? ` · ${timeText(item.created_at)}` : ''}</p>
      {managementPanel(item)}
      {!item.old_version && <p className="catalog-update-hint">本地首次归档：请核对云端是否已有此编号及版本；未找到条目时，请确认是否需要新增。</p>}
      {locationPanel(locationKey, item.new_version)}
      {(!locatedResult || locationExpired) && <p className="catalog-update-hint">{snapshotLocation ? '已有历史快照参考，但当前行号需点击读取飞书后确认。' : '请选定对应清单后读取最新位置。'}</p>}
      {data?.can_manage && locationDocument && <button className="secondary-button" type="button" disabled={!!locating[locationKey]} onClick={() => void locate(locationKey, locationDocument.token, item.code, done ? undefined : item)}>{locatedResult ? '重新读取并定位' : '读取飞书并定位'}</button>}
      <p className="catalog-update-filename">{item.file_name}</p>
      {!done && item.match_status !== 'matched' && <p className="catalog-update-hint">{item.match_status === 'ambiguous' ? ((!item.registrant || item.registrant === '待确认') ? '公司主体待确认，已按型号列出候选清单，请文控确认适用清单。' : '找到多个可能的清单，请文控确认对应关系后打开修改。') : '暂未找到唯一对应清单，请文控选择正确清单后打开修改。'}</p>}
      {!done && item.match_status === 'matched' && <p className="catalog-update-hint">已找到对应清单。具体条目请读取飞书定位；未找到时，请核对是否需要新增。</p>}
      <div className="catalog-update-actions">
        {item.document_url && <a className="primary-button" href={item.document_url} target="_blank" rel="noopener noreferrer">打开对应清单 ↗</a>}
        {item.download_url && <a className="secondary-button" href={item.download_url} download>下载新版文件</a>}
        {item.demo && <><button className="secondary-button" type="button" onClick={() => setDemoDone(value => !value)}>{demoDone ? '重新演示' : '模拟确认处理结果'}</button><button className="secondary-button" type="button" onClick={() => setShowDemo(false)}>关闭演示</button></>}
        {!item.demo && !done && data?.can_manage && <button className="secondary-button" type="button" disabled={busy || loading || Boolean(data.catalog_error)} onClick={() => begin(item)}>{item.match_status === 'matched' ? '确认处理结果' : '选择清单并处理'}</button>}
      </div>
      {done && <p className="catalog-update-receipt">{item.processed_by || '文控'} · {timeText(item.processed_at)}{item.note ? ` · ${item.note}` : ''}<br />仅记录文控确认，不代表系统核验飞书内容。</p>}
      {isEditing && <form className="catalog-update-form" onSubmit={event => { event.preventDefault(); void resolve(item); }}>
        <label htmlFor={`catalog-doc-${item.id}`}>确认对应清单</label>
        <select id={`catalog-doc-${item.id}`} value={documentToken} required disabled={busy || Boolean(item.targets?.length)} onChange={event => { setDocumentToken(event.currentTarget.value); setConfirmed(false); }}><option value="">请选择对应清单</option>{data?.catalog_documents.map(document => <option key={document.token} value={document.token}>{document.path.join(' / ') || document.name}</option>)}</select>
        {selectedDocument && <a className="catalog-update-form-link" href={selectedDocument.url} target="_blank" rel="noopener noreferrer">打开所选清单：{selectedDocument.name} ↗</a>}
        <label htmlFor={`catalog-note-${item.id}`}>{requiresNote ? '对应关系确认说明（必填）' : '处理说明（选填）'}</label>
        <textarea id={`catalog-note-${item.id}`} value={note} required={requiresNote} maxLength={1000} rows={2} disabled={busy} placeholder="例如：已将对应 BOM 版本更新为新版" onChange={event => setNote(event.currentTarget.value)} />
        <label className="catalog-update-confirm"><input type="checkbox" checked={confirmed} disabled={busy || !documentToken} onChange={event => setConfirmed(event.currentTarget.checked)} /><span>我已核对所选飞书清单，并完成必要修改或确认无需修改。仅记录文控确认，不代表系统核验飞书内容。</span></label>
        {!done && item.match_status === 'matched' && <p className="catalog-update-hint">已找到对应清单。具体条目请读取飞书定位；未找到时，请核对是否需要新增。</p>}
      <div className="catalog-update-actions"><button type="submit" className="primary-button" disabled={busy || !confirmed || !documentToken || (requiresNote && !note.trim())}>{busy ? '保存中…' : '我已核对并处理'}</button><button type="button" className="secondary-button" disabled={busy} onClick={() => setEditing(null)}>取消</button></div>
      </form>}
      </div>
    </details>;
  }

  return <section className="catalog-updates" aria-labelledby="catalog-updates-title">
    <div className="catalog-updates-heading"><div><p className="eyebrow">MANUAL CHECKLIST UPDATES</p><h2 id="catalog-updates-title">清单更新提醒{data && <span>{pending.length + (demoItem && !demoDone ? 1 : 0)} 个文件{demoItem && !demoDone ? '（含演示 1 条）' : ''}</span>}</h2><p>首次归档或更新后，按文件类型提示应核对的 DMR 清单、专用台账或配套资料。清单链接和条目定位分别展示；文控核对后手动处理，平台不自动写入云文档。</p></div><button type="button" className="secondary-button" disabled={loading || busy} onClick={() => setReload(value => value + 1)}>{loading ? '正在读取…' : '刷新提醒'}</button></div>
    {data?.can_manage && <details className="catalog-locate-test"><summary>定位测试 · 不上传也可验证清单位置</summary><form className="catalog-update-form" onSubmit={event => {event.preventDefault(); void locate('test', testDocument, testCode, undefined, {fileName: testFileName, module: testModule, version: testVersion});}}>
      <label htmlFor="locate-test-document">选择清单</label><select id="locate-test-document" value={testDocument} required disabled={!!locating.test} onChange={event => {setTestDocument(event.target.value); setLiveLocations(value => ({...value, test: {status:'idle'}}));}}><option value="">请选择清单</option>{data.catalog_documents.map(doc => <option key={doc.token} value={doc.token}>{doc.path.join(' / ')}</option>)}</select>
      <label htmlFor="locate-test-code">文件编号（选填）</label><input id="locate-test-code" value={testCode} maxLength={120} disabled={!!locating.test} placeholder="例如：11UI04AA01" onChange={event => {setTestCode(event.target.value); setLiveLocations(value => ({...value, test: {status:'idle'}}));}}/>
      <label htmlFor="locate-test-file-name">文件名（无编号时填写）</label><input id="locate-test-file-name" value={testFileName} maxLength={240} disabled={!!locating.test} placeholder="例如：UI04_CN_V1.0.4.00.ATK" onChange={event => {setTestFileName(event.target.value); setLiveLocations(value => ({...value, test: {status:'idle'}}));}}/>
      <label htmlFor="locate-test-module">文件类型（选填）</label><select id="locate-test-module" value={testModule} disabled={!!locating.test} onChange={event => {setTestModule(event.target.value); setLiveLocations(value => ({...value, test: {status:'idle'}}));}}><option value="">不限制文件类型</option>{modules.map(moduleName => <option key={moduleName} value={moduleName}>{moduleName}</option>)}</select>
      <label htmlFor="locate-test-version">拟更新版本（选填）</label><input id="locate-test-version" value={testVersion} maxLength={120} onChange={event => setTestVersion(event.target.value)} placeholder="例如：20260924"/>
      <div><button className="primary-button" disabled={!!locating.test || (!testCode.trim() && !testFileName.trim()) || !testDocument}>{locating.test ? '正在读取…' : '读取飞书并定位'}</button></div>
      {liveLocations.test?.status !== 'idle' && locationPanel('test', testVersion)}
      <small>仅定位和展示建议，不写入云清单，也不生成归档记录。</small>
    </form></details>}
    {notice && <p className="catalog-updates-message" role="status">{notice}</p>}
    {error && <p className="catalog-updates-error" role="alert">{error}</p>}
    {data?.catalog_error && <p className="catalog-updates-error" role="alert">清单目录暂不可用：{data.catalog_error}。请刷新后再确认处理。</p>}
    {loading && !data && <p className="catalog-updates-empty" role="status">正在读取待更新提醒…</p>}
    {data && !pending.length && (!demoItem || demoDone) && <p className="catalog-updates-empty">暂无待更新提醒。</p>}
    {data && !data.can_manage && pending.length > 0 && <p className="catalog-update-hint">请由有归档权限的文控人员确认处理。</p>}
    <div className="catalog-updates-list">{demoItem && !demoDone && renderItem(demoItem)}{pending.map(renderGroup)}</div>
    {(processed.length > 0 || (demoItem && demoDone)) && <details className="catalog-updates-history"><summary>文控已确认记录 · {processed.length + (demoItem && demoDone ? 1 : 0)} 条{demoItem && demoDone ? '（含演示 1 条）' : ''}</summary><div className="catalog-updates-list">{demoItem && demoDone && renderItem(demoItem)}{processed.map(renderGroup)}</div></details>}
  </section>;
}
