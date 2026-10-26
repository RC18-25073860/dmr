import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api, apiErrorMessage } from '../api';
import type { DmrRecord, NasAccountStatus, NasDirectoryMapping, PublicUser, ServerState, StorageServiceStatus, StorageSyncOverview } from '../types';
import { projectCategory } from './shared';

const statusLabels: Record<string, string> = {
  unverified: '尚未验证',
  pending: '等待验证',
  checking: '正在验证',
  verified: '验证通过',
  failed: '验证失败',
};

const syncStatusLabels: Record<string, string> = {
  pending: '等待保存',
  transferring: '正在保存',
  blocked: '需要处理',
  synced: '已同步',
};

const blankMapping = (): NasDirectoryMapping => ({
  registrant: '',
  project_category: '待确认',
  product: '',
  module: '',
  rule: '',
  target_path: '',
  enabled: false,
});

const normalizeMapping = (mapping: NasDirectoryMapping): NasDirectoryMapping => ({
  ...mapping,
  target_path: mapping.target_path || mapping.nas_path || '',
  enabled: mapping.enabled ?? mapping.active ?? false,
});

const recordLabel = (record: DmrRecord) => `${record.no || record.name} · ${record.registrant} / ${projectCategory(record)} / ${record.product} / ${record.module} / ${record.type}`;

async function nasRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers: {
      ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent('dmr:unauthorized'));
  }
  if (!response.ok) {
    const payload = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    const message = String(payload.error || payload.detail || payload.message || `请求失败 ${response.status}`);
    throw new ApiError(message, response.status, typeof payload.code === 'string' ? payload.code : undefined, data);
  }
  return data as T;
}

function formatTime(value?: string | null) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

export function NasAccountBadge({ user }: { user: PublicUser }) {
  const status = user.nas_check_status || '';
  if (user.nas_account_configured || user.nas_account) {
    return <span className={`status-badge ${status === 'failed' ? 'error' : status === 'verified' ? '' : 'warning'}`}>{statusLabels[status] || '已配置'}</span>;
  }
  return <span className="module-badge">未配置</span>;
}

export function NasAccountModal({ user, open, onClose, onChanged }: { user: PublicUser | null; open: boolean; onClose: () => void; onChanged: () => Promise<void> }) {
  const [account, setAccount] = useState('');
  const [password, setPassword] = useState('');
  const [status, setStatus] = useState<NasAccountStatus | null>(null);
  const [storage, setStorage] = useState<StorageServiceStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [clearing, setClearing] = useState(false);

  const load = async () => {
    if (!user) return;
    setMessage('');
    const [accountResult, storageResult] = await Promise.allSettled([
      api<NasAccountStatus>(`/api/users/${encodeURIComponent(user.id)}/nas-account`),
      api<StorageServiceStatus>('/api/storage-status'),
    ]);
    if (accountResult.status === 'fulfilled') {
      setStatus(accountResult.value);
      setAccount(accountResult.value.account || '');
    } else {
      setStatus(null);
      setAccount('');
      setMessage(apiErrorMessage(accountResult.reason));
    }
    setStorage(storageResult.status === 'fulfilled' ? storageResult.value : { configured: false, online: false, last_seen_at: null, message: apiErrorMessage(storageResult.reason) });
  };

  useEffect(() => {
    if (!open || !user) return;
    setPassword('');
    setClearing(false);
    void load().catch(error => setMessage(apiErrorMessage(error)));
  }, [open, user?.id]);

  useEffect(() => {
    if (!open || !user || !status || !['pending', 'checking'].includes(status.check_status)) return;
    const timer = window.setInterval(() => void load().catch(error => setMessage(apiErrorMessage(error))), 3000);
    return () => window.clearInterval(timer);
  }, [open, user?.id, status?.check_status]);

  const needsPassword = Boolean(!status?.password_configured || account.trim() !== (status?.account || ''));
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!user || busy) return;
    if (clearing) return;
    if (!account.trim()) {
      setMessage('请填写 192 账号。');
      return;
    }
    if (needsPassword && !password) {
      setMessage('首次配置或更换账号时需要填写密码。');
      return;
    }
    setBusy(true);
    setMessage('');
    try {
      const result = await nasRequest<NasAccountStatus>(`/api/users/${encodeURIComponent(user.id)}/nas-account`, {
        method: 'PUT',
        body: JSON.stringify({ account: account.trim(), password: password || null }),
      });
      setStatus(result);
      setAccount(result.account || '');
      setPassword('');
      setMessage(result.password_configured ? '账号已保存，密码已加密存储；请按需提交验证请求。' : '账号已保存。');
      await onChanged();
    } catch (error) {
      setMessage(apiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    if (!user || busy) return;
    setBusy(true);
    setMessage('');
    try {
      const result = await nasRequest<NasAccountStatus>(`/api/users/${encodeURIComponent(user.id)}/nas-account`, { method: 'DELETE' });
      setStatus(result);
      setAccount('');
      setPassword('');
      setClearing(false);
      setMessage('已清除系统内保存的 192 账号和密码，不会删除 NAS 账号或文件。');
      await onChanged();
    } catch (error) {
      setMessage(apiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    if (!user || busy) return;
    setBusy(true);
    setMessage('');
    try {
      const result = await nasRequest<NasAccountStatus>(`/api/users/${encodeURIComponent(user.id)}/nas-account/check`, { method: 'POST' });
      setStatus(result);
      setMessage('已提交验证请求，等待内网同步程序处理。');
      await onChanged();
    } catch (error) {
      setMessage(apiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const canCheck = Boolean(status?.password_configured && storage?.configured && !password && account === status.account && !['pending', 'checking'].includes(status.check_status));

  return <div className={`modal-backdrop ${open ? 'open' : ''}`} aria-hidden={!open} onClick={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    {user && <form className="permission-modal nas-account-modal" role="dialog" aria-modal="true" onSubmit={save}>
      <div className="modal-head">
        <div><p className="eyebrow">PERSONAL 192 ACCOUNT</p><h2>{user.display_name} · 192 账号</h2><p>这里绑定个人 NAS 账号。密码只提交给后端加密保存，页面不会回显。</p></div>
        <button type="button" aria-label="关闭" disabled={busy} onClick={onClose}>×</button>
      </div>
      <div className="modal-body permission-form">
        <div className="nas-service-box">
          <span className={`status-badge ${storage?.configured ? storage.online ? '' : 'warning' : 'error'}`}>{storage?.configured ? storage.online ? '内网服务在线' : '等待内网服务' : 'NAS 服务未配置'}</span>
          <p>{storage?.configured ? `最近心跳：${formatTime(storage.last_seen_at || storage.last_seen)}` : storage?.message || '未配置内网同步服务时不能验证账号，也不能显示验证成功。'}</p>
        </div>
        <div className="nas-account-state">
          <strong>{status?.password_configured ? '已保存凭据' : '尚未保存凭据'}</strong>
          <span>{status ? `${statusLabels[status.check_status] || status.check_status}${status.check_error ? ` · ${status.check_error}` : ''}` : '等待后端返回账号状态'}</span>
          <small>上次验证：{formatTime(status?.checked_at)}</small>
        </div>
        {clearing ? <div className="upload-rule-note"><i>!</i><p>确认清除后，仅删除本系统保存的账号密码和验证状态；不会删除 192 云盘账号或任何 NAS 文件。</p></div> : <>
          <div className="permission-form-grid">
            <label><span>192 账号</span><input value={account} onChange={event => { setAccount(event.currentTarget.value); setMessage(''); }} autoComplete="off" maxLength={200} /></label>
            <label><span>{status?.password_configured ? '更新密码' : '192 密码'}</span><input type="password" value={password} onChange={event => { setPassword(event.currentTarget.value); setMessage(''); }} autoComplete="new-password" placeholder={needsPassword ? '必须填写' : '留空保留原密码'} /></label>
          </div>
          <div className="upload-rule-note"><i>i</i><p>保存账号只说明平台已加密保存凭据；验证请求需要内网程序在线处理，验证通过也只代表账号可登录和访问 File Station。</p></div>
        </>}
        {message && <div className="form-message">{message}</div>}
      </div>
      <div className="modal-footer">
        <span>192 权限不改变 DMR 业务角色；查看和下载仍按本页人员范围控制。</span>
        <div>
          {status?.password_configured && <button type="button" className="secondary-button" disabled={busy} onClick={() => { setClearing(value => !value); setMessage(''); }}>{clearing ? '返回编辑' : '清除凭据'}</button>}
          {clearing ? <button type="button" className="primary-button" disabled={busy} onClick={() => void clear()}>{busy ? '处理中...' : '确认清除'}</button> : <>
            <button type="button" className="secondary-button" disabled={busy || !canCheck} onClick={() => void check()}>{busy ? '处理中...' : '请求验证'}</button>
            <button className="primary-button" disabled={busy}>{busy ? '保存中...' : '保存账号'}</button>
          </>}
        </div>
      </div>
    </form>}
  </div>;
}

export function NasStoragePanel() {
  const [overview, setOverview] = useState<StorageSyncOverview | null>(null);
  const [message, setMessage] = useState('');
  const [busyId, setBusyId] = useState('');

  const load = async () => {
    setMessage('');
    try {
      setOverview(await api<StorageSyncOverview>('/api/admin/storage-sync'));
    } catch (error) {
      setOverview(null);
      setMessage(apiErrorMessage(error));
    }
  };

  useEffect(() => { void load(); const timer = window.setInterval(() => void load(), 5000); return () => window.clearInterval(timer); }, []);

  const retry = async (id: string, confirm: boolean) => {
    setBusyId(id);
    setMessage('');
    try {
      await nasRequest(`/api/admin/storage-sync/files/${encodeURIComponent(id)}/retry`, { method: 'POST', body: JSON.stringify({ confirm_account_change: confirm }) });
      await load();
    } catch (error) {
      setMessage(apiErrorMessage(error));
    } finally {
      setBusyId('');
    }
  };

  const files = overview?.jobs || overview?.files || [];
  return <section className="table-panel nas-panel nas-storage-panel"><details className="nas-storage-details">
    <summary className="panel-head padded nas-storage-summary">
      <div><p className="eyebrow">192 STORAGE SERVICE</p><h2>192 存储状态 <small>{files.length} 条记录</small></h2></div>
      <span className={`status-badge ${overview?.configured ? overview.online ? '' : 'warning' : 'error'}`}>{overview?.configured ? overview.online ? '内网服务在线' : '等待内网服务' : '未配置'}</span>
      <span className="nas-storage-toggle"><span className="nas-show">展开明细</span><span className="nas-hide">收起明细</span><span aria-hidden="true">⌄</span></span>
    </summary>
    <div className="nas-panel-body">
      <p className="nas-muted">{overview ? `最近心跳：${formatTime(overview.last_seen_at || overview.last_seen)}。归档后才会生成同步任务，写入使用提交人的个人 192 账号。` : message || '正在读取存储状态。'}</p>
      {message && <div className="form-message">{message}</div>}
      <div className="table-scroll nas-storage-scroll" tabIndex={0} role="region" aria-label="192 存储任务明细"><table><colgroup><col style={{width: '38%'}}/><col style={{width: '14%'}}/><col style={{width: '14%'}}/><col style={{width: '24%'}}/><col style={{width: '10%'}}/></colgroup><thead><tr><th>文件</th><th>提交人</th><th>状态</th><th>说明</th><th>操作</th></tr></thead><tbody>
        {files.length ? files.map(file => <tr key={file.id}>
          <td><span className="cell-main">{file.name || file.target_name || file.file_id}</span><span className="cell-sub">尝试次数：{file.attempts || 0}</span></td>
          <td>{file.submitted_by || file.owner_display_name || '—'}</td>
          <td><span className={`status-badge ${file.status === 'blocked' ? 'error' : file.status === 'synced' ? '' : 'warning'}`}>{syncStatusLabels[file.status] || file.status}</span></td>
          <td>{file.error || file.last_error || file.target_path || '—'}</td>
          <td>{file.status === 'blocked' ? <button className="row-button" disabled={busyId === (file.file_id || file.id)} onClick={() => void retry(file.file_id || file.id, Boolean(file.account_changed))}>{busyId === (file.file_id || file.id) ? '处理中' : file.account_changed ? '确认账号并重试' : '重试'}</button> : '—'}</td>
        </tr>) : <tr><td colSpan={5} className="empty-row">暂无待同步或异常文件。</td></tr>}
      </tbody></table></div>
    </div>
  </details></section>;
}

type DirectoryCreationJob = {
  id: string;
  record_id: string;
  nas_path: string;
  status: 'pending' | 'creating' | 'succeeded' | 'failed';
  last_error: string;
};

const directoryStatusLabels = {
  pending: '等待内网服务创建',
  creating: '正在创建目录',
  succeeded: '已创建并关联',
  failed: '创建未完成',
};

function NasCreateDirectory({ records, onLinked }: { records: DmrRecord[]; onLinked: (recordIds: string[]) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [recordId, setRecordId] = useState('');
  const [parentPath, setParentPath] = useState('');
  const [folderName, setFolderName] = useState('');
  const [jobs, setJobs] = useState<DirectoryCreationJob[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [messageIsError, setMessageIsError] = useState(false);
  const [loadError, setLoadError] = useState('');
  const linked = useRef(new Set<string>());
  const onLinkedRef = useRef(onLinked);
  onLinkedRef.current = onLinked;

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const result = await api<{ jobs: DirectoryCreationJob[] }>('/api/nas/directory-creation-jobs');
        if (stopped) return;
        setJobs(result.jobs);
        setLoadError('');
        const completed = result.jobs.filter(job => job.status === 'succeeded' && !linked.current.has(job.id));
        if (completed.length) {
          await onLinkedRef.current(completed.map(job => job.record_id));
          completed.forEach(job => linked.current.add(job.id));
        }
      } catch (error) {
        if (!stopped) setLoadError(apiErrorMessage(error));
      } finally {
        if (!stopped) timer = setTimeout(refresh, 4000);
      }
    };
    void refresh();
    return () => { stopped = true; clearTimeout(timer); };
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setMessage('');
    setMessageIsError(true);
    const name = folderName.trim();
    const parent = parentPath.trim().replace(/\/+$/, '');
    if (!recordId || !parent.startsWith('/') || !name || name === '.' || name === '..' || /[\\/\x00-\x1f]/.test(name)) {
      setMessage('请选择 DMR 记录，填写已有父目录的完整路径和单层新目录名称。');
      return;
    }
    setBusy(true);
    try {
      const result = await nasRequest<{ job: DirectoryCreationJob }>('/api/nas/directory-creation-jobs', {
        method: 'POST', body: JSON.stringify({ record_id: recordId, parent_path: parent, folder_name: name }),
      });
      setJobs(items => [result.job, ...items.filter(item => item.id !== result.job.id)]);
      setMessageIsError(false);
      setMessage('创建请求已提交。待 192 确认目录后才会保存关联，可在下方查看结果。');
    } catch (error) {
      setMessage(apiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return <div className="nas-directory-create">
    <button type="button" className="row-button" aria-expanded={open} onClick={() => setOpen(value => !value)}>新建 192 目录并关联</button>
    {open && <form className="nas-directory-form" onSubmit={event => void submit(event)}>
      <p className="nas-muted">在已有父目录下新建一层目录，使用你绑定的个人 192 账号执行。目录创建成功后才关联所选记录；失败时保留原有映射和本地文件。</p>
      <div className="nas-mapping-row">
        <label><span>关联 DMR 记录</span><select aria-label="关联 DMR 记录" required value={recordId} disabled={busy} onChange={event => setRecordId(event.currentTarget.value)}><option value="">请选择已有 DMR 记录</option>{records.map(record => <option key={record.id} value={record.id}>{recordLabel(record)}</option>)}</select></label>
        <label><span>192 已有父目录</span><input required disabled={busy} value={parentPath} onChange={event => setParentPath(event.currentTarget.value)} placeholder="/体系文件（受控发布版）/产品资料" /></label>
        <label><span>新目录名称</span><input required disabled={busy} value={folderName} onChange={event => setFolderName(event.currentTarget.value)} placeholder="例如：新产品名称" /></label>
        <div className="nas-mapping-meta"><span>将创建并关联到</span><strong>{parentPath.trim() && folderName.trim() ? `${parentPath.trim().replace(/\/+$/, '')}/${folderName.trim()}` : '填写后显示完整目标目录'}</strong></div>
      </div>
      <div className="nas-panel-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? '正在提交…' : '创建目录并关联'}</button></div>
      {message && <div className={`form-message ${messageIsError ? '' : 'nas-directory-info'}`} role={messageIsError ? 'alert' : 'status'}>{message}</div>}
    </form>}
    {loadError && <div className="form-message" role="status">目录创建状态暂不可用：{loadError}</div>}
    {jobs.length > 0 && <div className="nas-directory-jobs" aria-label="目录创建记录">
      {jobs.map(job => <div className="nas-directory-job" key={job.id}>
        <div><strong>{records.find(record => record.id === job.record_id)?.no || job.record_id}</strong><p>{job.nas_path}</p></div>
        <span className={`status-badge ${job.status === 'failed' ? 'error' : job.status === 'succeeded' ? '' : 'warning'}`}>{directoryStatusLabels[job.status]}</span>
        {job.last_error && <p className="nas-directory-error">{job.last_error}</p>}
        {job.status === 'failed' && <button className="row-button" type="button" onClick={() => {
          const split = job.nas_path.lastIndexOf('/');
          setRecordId(job.record_id); setParentPath(job.nas_path.slice(0, split)); setFolderName(job.nas_path.slice(split + 1)); setOpen(true); setMessageIsError(false); setMessage('已填入原请求，请核对失败原因并处理后重新提交。');
        }}>重新填写</button>}
      </div>)}
    </div>}
  </div>;
}

export function NasDirectoryMappingsPanel() {
  const [mappings, setMappings] = useState<NasDirectoryMapping[]>([]);
  const [records, setRecords] = useState<DmrRecord[]>([]);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string[]>([]);
  const [draft, setDraft] = useState<NasDirectoryMapping | null>(null);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkPath, setBulkPath] = useState('');
  const [loaded, setLoaded] = useState(false);

  const load = async () => {
    try {
      const [mappingResult, state] = await Promise.all([
        api<{ mappings: NasDirectoryMapping[] }>('/api/nas/directory-mappings'), api<ServerState>('/api/state'),
      ]);
      setMappings(mappingResult.mappings.map(normalizeMapping));
      setRecords(state.records || []);
      setLoaded(true);
    } catch (error) { setMessage(apiErrorMessage(error)); }
  };
  useEffect(() => { void load(); }, []);

  const upsert = (mapping: NasDirectoryMapping) => setMappings(items => {
    const normalized = normalizeMapping(mapping);
    return items.some(item => item.record_id === normalized.record_id)
      ? items.map(item => item.record_id === normalized.record_id ? normalized : item)
      : [normalized, ...items];
  });
  const save = async () => {
    if (!draft || busy) return;
    if (!draft.record_id || !draft.target_path.trim()) { setMessage('请选择已有 DMR 记录并填写目标目录。'); return; }
    if (!draft.id && !draft.enabled) { setMessage('新规则请勾选启用后保存。'); return; }
    setBusy(true); setMessage('');
    try {
      if (draft.enabled) {
        const result = await nasRequest<{ mapping: NasDirectoryMapping }>('/api/nas/directory-mappings', { method: 'POST', body: JSON.stringify({record_id: draft.record_id, nas_path: draft.target_path.trim()}) });
        upsert(result.mapping);
      } else {
        await nasRequest(`/api/nas/directory-mappings/${encodeURIComponent(draft.record_id)}`, {method: 'DELETE'});
        upsert({...mappings.find(item => item.record_id === draft.record_id)!, enabled: false, active: false});
      }
      setDraft(null); setMessage('目录映射已保存；已有文件不会移动或删除。');
    } catch (error) { setMessage(apiErrorMessage(error)); } finally { setBusy(false); }
  };
  const saveBulk = async () => {
    if (busy || !selected.length || !bulkPath.trim()) return;
    setBusy(true); setMessage('');
    let completed = 0;
    try {
      for (const recordId of selected) {
        const result = await nasRequest<{ mapping: NasDirectoryMapping }>('/api/nas/directory-mappings', {method: 'POST', body: JSON.stringify({record_id: recordId, nas_path: bulkPath.trim()})});
        upsert(result.mapping); completed++;
        setSelected(ids => ids.filter(id => id !== recordId));
      }
      setBulkOpen(false); setBulkPath(''); setMessage(`已为 ${completed} 条规则保存并启用共同目录。`);
    } catch (error) { setMessage(`已保存 ${completed} 条，其余仍保留勾选：${apiErrorMessage(error)}`); }
    finally { setBusy(false); }
  };
  const filtered = mappings.filter(item => [item.record_no, item.registrant, item.product, item.module, item.target_path, records.find(r => r.id === item.record_id)?.no].join(' ').toLowerCase().includes(query.trim().toLowerCase()));
  const pages = Math.max(1, Math.ceil(filtered.length / 10));
  const currentPage = Math.min(page, pages);
  const visible = filtered.slice((currentPage - 1) * 10, currentPage * 10);
  const pageIds = visible.map(item => item.record_id || '').filter(Boolean);
  const allPageSelected = pageIds.length > 0 && pageIds.every(id => selected.includes(id));
  const startEdit = (item?: NasDirectoryMapping) => { setDraft(item ? {...item} : {...blankMapping(), enabled: true}); setBulkOpen(false); setMessage(''); };

  return <section className="table-panel nas-panel nas-storage-panel nas-mapping-panel"><details className="nas-storage-details">
    <summary className="panel-head padded nas-storage-summary"><div><p className="eyebrow">NAS TARGET PATHS</p><h2>192 目录映射 <small>{loaded ? `${mappings.length} 条规则` : '正在读取'}</small></h2></div><span className="nas-storage-toggle"><span className="nas-show">展开管理</span><span className="nas-hide">收起管理</span><span aria-hidden="true">⌄</span></span></summary>
    <div className="nas-panel-body">
      <p className="nas-muted">按 DMR 记录关联 192 目录。普通保存只设置映射，不会创建目录或移动已有文件。</p>
      {message && <div className="form-message" role="status">{message}</div>}
      <div className="nas-mapping-toolbar"><input aria-label="搜索目录映射" type="search" placeholder="搜索编号、公司、机型、目录" value={query} onChange={event => {setQuery(event.target.value); setPage(1);}}/><button className="row-button" disabled={busy || !loaded} onClick={() => startEdit()}>新增规则</button><button className="row-button" disabled={busy || !selected.length} onClick={() => {setBulkOpen(true); setDraft(null);}}>批量设置共同目录（{selected.length}）</button>{selected.length > 0 && <button className="row-button" disabled={busy} onClick={() => setSelected([])}>清空勾选</button>}</div>
      {bulkOpen && <form className="nas-mapping-editor" onSubmit={event => {event.preventDefault(); void saveBulk();}}><h3>批量设置共同目录 · 已选 {selected.length} 条</h3><p className="nas-muted">包含跨页勾选的规则。保存后，这些规则会全部启用并使用下方目录；已有文件不会移动。</p><label>共同目标目录<input required value={bulkPath} disabled={busy} onChange={event => setBulkPath(event.target.value)} placeholder="/体系文件（受控发布版）/..."/></label><div className="nas-mapping-toolbar"><button className="primary-button" disabled={busy || !selected.length}>{busy ? '保存中…' : '保存并启用所选规则'}</button><button className="row-button" type="button" disabled={busy} onClick={() => setBulkOpen(false)}>取消</button></div></form>}
      {draft && <form className="nas-mapping-editor" onSubmit={event => {event.preventDefault(); void save();}}><h3>{draft.id ? '编辑映射' : '新增映射'}</h3><fieldset disabled={busy}>
        <label>已有 DMR 记录<select required value={draft.record_id || ''} disabled={Boolean(draft.id)} onChange={event => {const record = records.find(r => r.id === event.target.value); setDraft({...draft, record_id: event.target.value, registrant: record?.registrant || '', product: record?.product || '', module: record?.module || '', project_category: record ? projectCategory(record) : '待确认'});}}><option value="">请选择已有 DMR 记录</option>{records.map(record => <option key={record.id} value={record.id}>{recordLabel(record)}</option>)}</select></label>
        <label>目标目录<input required value={draft.target_path} onChange={event => setDraft({...draft, target_path: event.target.value})} placeholder="/体系文件（受控发布版）/..."/></label>
        <label className="nas-check-row"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({...draft, enabled: event.target.checked})}/>启用</label>
        <div className="nas-mapping-toolbar"><button className="primary-button">{busy ? '保存中…' : '保存目录映射'}</button><button type="button" className="row-button" onClick={() => setDraft(null)}>取消</button></div>
      </fieldset></form>}
      <div className="table-scroll nas-mapping-table"><table><thead><tr><th><input type="checkbox" aria-label="选择本页规则" disabled={busy || !pageIds.length} checked={allPageSelected} onChange={event => setSelected(ids => event.target.checked ? [...new Set([...ids, ...pageIds])] : ids.filter(id => !pageIds.includes(id)))}/></th><th>编号 / 归属</th><th>目标目录</th><th>状态</th><th>操作</th></tr></thead><tbody>{visible.map(mapping => <tr key={mapping.record_id}><td><input type="checkbox" aria-label={`选择规则 ${mapping.record_no || mapping.record_id}`} disabled={busy} checked={selected.includes(mapping.record_id || '')} onChange={event => setSelected(ids => event.target.checked ? [...new Set([...ids, mapping.record_id || ''])] : ids.filter(id => id !== mapping.record_id))}/></td><td><strong>{mapping.record_no || records.find(r => r.id === mapping.record_id)?.no || '未提供编号'}</strong><small className="cell-sub">{[mapping.registrant, mapping.product, mapping.module].filter(Boolean).join(' / ')}</small></td><td>{mapping.target_path || '未设置'}</td><td><span className={`status-badge ${mapping.enabled ? '' : 'warning'}`}>{mapping.enabled ? '启用' : '停用'}</span></td><td><button className="row-button" disabled={busy} onClick={() => startEdit(mapping)}>编辑</button></td></tr>)}</tbody></table>{!visible.length && <p className="empty-row">{loaded ? query ? '没有匹配的映射规则。' : '暂无映射，点击“新增规则”设置。' : '正在读取…'}</p>}</div>
      <div className="nas-mapping-pagination"><span>共 {filtered.length} 条 · 每页 10 条 · 第 {currentPage}/{pages} 页</span><button className="row-button" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}>上一页</button><button className="row-button" disabled={currentPage >= pages} onClick={() => setPage(currentPage + 1)}>下一页</button></div>
      <NasCreateDirectory records={records} onLinked={async () => {await load();}}/>
    </div>
  </details></section>;
}
