import { ShareFile } from './components/ShareFile';
import { matchesRecord, matchesText } from './recordSearch';
import { ArchiveCorrection } from './components/ArchiveCorrection';

import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiErrorMessage, authEventName, isApiError } from './api';
import { Dashboard } from './components/Dashboard';
import { Register } from './components/Register';
import { FilesView } from './components/FilesView';
import { Upload } from './components/Upload';
import { Logs } from './components/Logs';
import { Drawers } from './components/Drawers';
import { LoginGate } from './components/LoginGate';
import { Users } from './components/Users';
import type { BatchItem, DmrRecord, LibraryFile, PermissionName, PublicUser, ServerState, UploadStage, ViewName } from './types';
import { projectCategory } from './components/shared';
import './legacy.css';
import './ulike-theme.css';

const views: ViewName[] = ['dashboard', 'register', 'files', 'upload', 'logs', 'users'];
const viewLabels: Record<ViewName, string> = { dashboard: '工作台', register: 'DMR 清单', files: '受控文件', upload: '批量录入', logs: '日志记录', users: '人员与权限' };
const emptyState: ServerState = { records: [], libraryFiles: [], exceptions: [], operationLogs: [], notifications: [], batches: [], latestSyncChanges: [], latestSyncTime: '', latestSyncBatch: '', latestSyncTrigger: '本地检查', latestConfirmed: true, latestConfirmedBy: '', permissions: [] };

declare global { interface Window { __DMR_TEST_STATE__?: Record<string, unknown>; } }

const todayText = () => new Date().toLocaleDateString('sv-SE');
const readAsBase64 = (file: File) => new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1] || ''); reader.onerror = () => reject(new Error(`无法读取 ${file.name}`)); reader.readAsDataURL(file); });
type TransferManifestResult = { receipt_id: string; archive_status: string; network_path: string; file_date: string; filename: string; already_exists?: boolean };
const isTransferManifest = (name: string) => /^(?:ECN-\d+[:：]\s*)?DMR文件转移清单/i.test(name.trim());

function PasswordChangeGate({ user, forced, onChanged, onCancel }: { user: PublicUser; forced?: boolean; onChanged: (message: string) => void; onCancel?: () => void }) {
  const [form, setForm] = useState({ current_password: '', new_password: '', confirm_password: '' });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (!form.current_password || !form.new_password) {
      setMessage('请填写当前密码和新密码。');
      return;
    }
    if (form.new_password !== form.confirm_password) {
      setMessage('两次输入的新密码不一致。');
      return;
    }
    setBusy(true);
    setMessage('');
    try {
      await api('/api/auth/change-password', { current_password: form.current_password, new_password: form.new_password });
      onChanged('密码已更新，请用新密码重新登录。');
    } catch (error) {
      setMessage(apiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return <div className={forced ? 'login-shell' : 'password-modal-shell'}>
    <section className="login-card">
      <div className="brand"><div className="brand-seal">DMR</div><div><strong>量产文件管理</strong><small>LOCAL ACCESS</small></div></div>
      <div className="login-body">
        <p className="eyebrow">{forced ? 'PASSWORD REQUIRED' : 'CHANGE PASSWORD'}</p>
        <h1>{forced ? '首次登录需要修改密码' : '修改个人密码'}</h1>
        <p>{user.display_name}，修改成功后系统会退出当前会话，请重新登录。</p>
        <form className="login-form" onSubmit={submit}>
          <label><span>当前密码</span><input type="password" value={form.current_password} onChange={event => { const value = event.currentTarget.value; setForm(item => ({ ...item, current_password: value })); }} autoComplete="current-password" /></label>
          <label><span>新密码</span><input type="password" value={form.new_password} onChange={event => { const value = event.currentTarget.value; setForm(item => ({ ...item, new_password: value })); }} autoComplete="new-password" /></label>
          <label><span>确认新密码</span><input type="password" value={form.confirm_password} onChange={event => { const value = event.currentTarget.value; setForm(item => ({ ...item, confirm_password: value })); }} autoComplete="new-password" /></label>
          {message && <div className="form-message">{message}</div>}
          <div className="login-form-actions">
            {!forced && onCancel && <button type="button" className="secondary-button" onClick={onCancel}>取消</button>}
            <button className="primary-button" disabled={busy}>{busy ? '保存中...' : '保存并重新登录'}</button>
          </div>
        </form>
      </div>
    </section>
  </div>;
}

export function App() {
  const [state, setState] = useState<ServerState>(emptyState);
  const [view, setViewRaw] = useState<ViewName>(() => { const hash = location.hash.slice(1); return hash === 'sync' ? 'logs' : views.includes(hash as ViewName) ? hash as ViewName : 'dashboard'; });
  const [ready, setReady] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [authUser, setAuthUser] = useState<PublicUser | null>(null);
  const [authNotice, setAuthNotice] = useState('');
  const [passwordPanelOpen, setPasswordPanelOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [toast, setToast] = useState<Array<{ id: number; title: string; detail: string }>>([]);
  const [drawerRecordId, setDrawerRecordId] = useState<string | null>(null);
  const [drawerFileId, setDrawerFileId] = useState<string | null>(null);
  const [notificationOpen, setNotificationOpen] = useState(false);
  const [exceptionId, setExceptionId] = useState<string | null>(null);
  const [syncOpen, setSyncOpen] = useState(false);
  const [catalogSearch, setCatalogSearch] = useState<import('./components/CatalogSearchResults').CatalogSearch | null>(null);
  const [finder, setFinder] = useState({ registrant: 'all', module: 'all', product: 'all', projectCategory: 'all', query: '' });
  const [registerFilter, setRegisterFilter] = useState({ q: '', registrant: 'all', type: 'all', product: 'all', module: 'all' });
  const [folderKey, setFolderKey] = useState('all');
  const [fileScope, setFileScope] = useState({ registrant: 'all', projectCategory: 'all', product: 'all', module: 'all' });
  const [fileSearchRevision, setFileSearchRevision] = useState(0);
  const [fileSearch, setFileSearch] = useState('');
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [logFilter, setLogFilter] = useState({ q: '', user: 'all', action: 'all', scope: 'all' });
  const [initialLatestOnly, setInitialLatestOnly] = useState(true);
  const [uploadStage, setUploadStage] = useState<UploadStage>('select');
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadBatchId, setUploadBatchId] = useState('');
  const [pendingUploadFiles, setPendingUploadFiles] = useState<BatchItem[]>([]);
  const [transferResults, setTransferResults] = useState<TransferManifestResult[]>([]);
  const [currentUploadId, setCurrentUploadId] = useState<string | null>(null);
  const [manualChoice, setManualChoice] = useState('new');
  const [manualFields, setManualFields] = useState<Record<string, string>>({});
  const [manualNote, setManualNote] = useState('');
  const [sameVersionRevision, setSameVersionRevision] = useState(false);
  const [manualVersion, setManualVersion] = useState('');
  const [manualProjectCategory, setManualProjectCategory] = useState('待确认');
  const [manualEditToken, setManualEditToken] = useState('');
  const [archiveSubjects, setArchiveSubjects] = useState<string[]>([]);
  const [shareFileId, setShareFileId] = useState<string | null>(null);
  const [correctionFileId, setCorrectionFileId] = useState<string | null>(null);
  const [recordProjectCategory, setRecordProjectCategory] = useState('待确认');
  const [recordProjectEdit, setRecordProjectEdit] = useState({ recordId: '', token: '' });
  const [lastUploadResult, setLastUploadResult] = useState<{ matched: number; skipped: number; manualUpdates?: number; archivedFiles?: Array<{ id: string; name: string; location: string }> }>({ matched: 0, skipped: 0 });
  const autoRestoreChecked = useRef(false);
  const activeUserRef = useRef<PublicUser | null>(null);
  const uploadBatchIdRef = useRef('');
  const batchAccessCheckRef = useRef('');

  const permissions = useMemo(() => new Set<PermissionName>(state.permissions || []), [state.permissions]);
  const can = (permission: PermissionName) => permissions.has(permission);
  const currentUser = state.currentUser || authUser;
  const canRead = can('read');
  const canDownload = can('download');
  const canPreview = can('preview');
  const canAnalyze = can('analyze');
  const canReview = can('review');
  const canCommit = can('commit');
  const canClassify = can('classify');
  const canCorrectArchive = can('correct_archive');
  const canSync = can('sync');
  const canResolve = can('resolve');
  const canNotify = can('notify');
  const canManageUsers = can('manage_users');
  const canHistory = can('history');
  const canAudit = can('audit');

  const records = canRead ? state.records : [];
  const files = (canRead ? state.libraryFiles : []).filter(file => canHistory || file.status !== '历史版本');
  const openExceptions = state.exceptions.filter(item => item.status === 'open');
  const shareFile = files.find(file => file.id === shareFileId);
  const shareRecord = records.find(record => record.id === shareFile?.recordId);
  const correctionFile = files.find(file => file.id === correctionFileId);
  const correctionRecord = records.find(record => record.id === correctionFile?.recordId);
  const selectedRecord = records.find(record => record.id === drawerRecordId);
  const selectedDrawerFile = drawerFileId ? files.find(file => file.id === drawerFileId) : files.find(file => file.recordId === selectedRecord?.id && file.status === '当前有效');
  const currentUploadItem = pendingUploadFiles.find(item => item.id === currentUploadId);
  const selectedException = openExceptions.find(item => item.id === exceptionId);
  const todayUpdateCount = records.filter(record => String(record.date).startsWith(todayText())).length;

  const showToast = (title: string, detail: string) => { const id = Date.now() + Math.random(); setToast(items => [...items, { id, title, detail }]); window.setTimeout(() => setToast(items => items.filter(item => item.id !== id)), 3600); };
  const fail = (error: unknown) => showToast(isApiError(error) && [409, 428].includes(error.status) ? '请刷新后重新核对' : '操作未完成', apiErrorMessage(error));
  const isAuthInvalid = (error: unknown) => isApiError(error) && [401, 403].includes(error.status);
  const loadState = async () => { const nextState = await api<ServerState>('/api/state'); const catalog = await api<{ subjects: string[] }>('/api/archive-subjects'); setArchiveSubjects(catalog.subjects); setState({ ...nextState, permissions: nextState.permissions || [] }); if (nextState.currentUser) setAuthUser(nextState.currentUser); return nextState; };
  const resetUploadDraft = (allowAutoRestore = true) => { autoRestoreChecked.current = !allowAutoRestore; setCurrentUploadId(null); setManualEditToken(''); setPendingUploadFiles([]); setUploadBatchId(''); setUploadStage('select'); setUploadProgress(0); };
  const clearAuth = () => { setAuthUser(null); setState(emptyState); setReady(false); setPasswordPanelOpen(false); document.documentElement.dataset.ready = 'false'; setDrawerRecordId(null); setDrawerFileId(null); setNotificationOpen(false); setExceptionId(null); setRecordProjectEdit({ recordId: '', token: '' }); resetUploadDraft(); };
  const handlePasswordChanged = (message: string) => { setAuthNotice(message); clearAuth(); setAuthReady(true); };
  const bootstrap = async (user?: PublicUser) => { if (user) setAuthUser(user); const target = user || authUser; if (target?.must_change_password) { setReady(false); setAuthReady(true); document.documentElement.dataset.ready = 'false'; return; } await loadState(); setReady(true); setAuthReady(true); document.documentElement.dataset.ready = 'true'; };
  const refreshSession = async () => {
    try {
      const result = await api<{ user: PublicUser }>('/api/auth/me');
      if (!result.user?.active) { clearAuth(); return; }
      setAuthUser(result.user);
      if (result.user.must_change_password) { setReady(false); document.documentElement.dataset.ready = 'false'; return; }
      try {
        await loadState();
        setReady(true);
        document.documentElement.dataset.ready = 'true';
      } catch (error) {
        if (isAuthInvalid(error)) clearAuth();
        else if (activeUserRef.current) fail(error);
      }
    } catch (error) {
      if (isAuthInvalid(error)) clearAuth();
      else if (activeUserRef.current) fail(error);
    } finally {
      setAuthReady(true);
    }
  };
  const setView = (next: ViewName) => { setViewRaw(next); location.hash = next; setSidebarOpen(false); };
  const recordById = (id?: string) => records.find(record => record.id === id || record.no === id);
  const fileRecord = (file?: LibraryFile) => recordById(file?.recordId || file?.recordNo);


  const registerRows = useMemo(() => records.filter(record => matchesRecord(record, registerFilter.q, files) && (registerFilter.registrant === 'all' || record.registrant === registerFilter.registrant) && (registerFilter.type === 'all' || projectCategory(record) === registerFilter.type) && (registerFilter.product === 'all' || record.product === registerFilter.product) && (registerFilter.module === 'all' || record.module === registerFilter.module)), [records, files, registerFilter]);
  const folderParts = useMemo(() => { try { return folderKey === 'all' ? [] : JSON.parse(folderKey) as string[]; } catch { return []; } }, [folderKey]);
  const visibleFiles = useMemo(() => files.filter(file => { const record = fileRecord(file); return record && (fileScope.registrant === 'all' || record.registrant === fileScope.registrant) && (fileScope.projectCategory === 'all' || projectCategory(record) === fileScope.projectCategory) && (fileScope.product === 'all' || record.product === fileScope.product) && (fileScope.module === 'all' || record.module === fileScope.module) && (!folderParts[0] || record.registrant === folderParts[0]) && (!folderParts[1] || projectCategory(record) === folderParts[1]) && (!folderParts[2] || record.product === folderParts[2]) && (!folderParts[3] || record.module === folderParts[3]) && matchesText(fileSearch, [file.name, file.recordNo, file.version, record.name, record.product, record.registrant, record.module]); }), [files, records, folderParts, fileSearch, fileScope]);
  const selectedFile = visibleFiles.find(file => file.id === selectedFileId) || visibleFiles[0];
  const visibleLogs = useMemo(() => { const q = logFilter.q; return state.operationLogs.filter(log => matchesText(q, Object.values(log)) && (logFilter.user === 'all' || log.user === logFilter.user) && (logFilter.action === 'all' || log.action === logFilter.action) && (logFilter.scope === 'all' || (logFilter.scope === 'sync' && log.action === '同步数据') || (logFilter.scope === 'manual' && log.user !== '系统自动任务') || (logFilter.scope === 'system' && log.user === '系统自动任务'))); }, [state.operationLogs, logFilter]);

  const restoreBatch = async (id: string) => { if (!id) return [] as BatchItem[]; const batch = await api<{ batch_id?: string; id?: string; items: BatchItem[] }>(`/api/batches/${encodeURIComponent(id)}`); const items = batch.items || []; setUploadBatchId(batch.batch_id || batch.id || id); setPendingUploadFiles(items); setUploadStage('review'); setUploadProgress(100); return items; };
  const openRecord = (id: string) => { const record = recordById(id); if (!record) return; setDrawerRecordId(record.id); setRecordProjectCategory(projectCategory(record)); setRecordProjectEdit({ recordId: record.id, token: record.edit_token || '' }); setDrawerFileId(files.find(file => file.recordId === record.id && file.status === '当前有效')?.id || null); };
  const openFile = (file: LibraryFile) => { const record = fileRecord(file); if (!record) return; setDrawerRecordId(record.id); setRecordProjectCategory(projectCategory(record)); setRecordProjectEdit({ recordId: record.id, token: record.edit_token || '' }); setDrawerFileId(file.id); };
  const openArchivedFile = (id: string) => { if (!files.some(file => file.id === id)) return showToast('文件暂不可见', '请刷新后再查看，或联系管理员核对资料范围。'); setFileScope({ registrant: 'all', projectCategory: 'all', product: 'all', module: 'all' }); setFolderKey('all'); setFileSearch(''); setFileSearchRevision(value => value + 1); setSelectedFileId(id); setView('files'); };
  const openUploadReview = (itemOrId: BatchItem | string) => { const item = typeof itemOrId === 'string' ? pendingUploadFiles.find(row => row.id === itemOrId) : itemOrId; if (!item) return; const matchedRecord = recordById(item.recordId || item.contextRecordId); setManualFields(Object.fromEntries(['code','registrant','product','module'].map(key => [key, String((item as unknown as Record<string, unknown>)[key] || '').replace(/^待确认$/, '')]))); setManualNote(item.metadata?.review_note || ''); setSameVersionRevision(false); setCurrentUploadId(item.id); setManualEditToken(item.edit_token || ''); setManualChoice(item.recordId ? `record:${item.recordId}` : item.contextRecordId ? `context:${item.contextRecordId}` : 'new'); setManualVersion(item.version || item.detected || ''); setManualProjectCategory(projectCategory(item.project_category ? { project_category: item.project_category } : matchedRecord)); };

  useEffect(() => { void refreshSession(); const onHash = () => { const rawHash = location.hash.slice(1); if (rawHash === 'sync') setViewRaw('logs'); else if (views.includes(rawHash as ViewName)) setViewRaw(rawHash as ViewName); }; const onUnauthorized = () => clearAuth(); const onRefresh = () => { if (!document.hidden) void refreshSession(); }; const timer = window.setInterval(() => { void refreshSession(); }, 25000); window.addEventListener('hashchange', onHash); window.addEventListener(authEventName, onUnauthorized); window.addEventListener('focus', onRefresh); document.addEventListener('visibilitychange', onRefresh); return () => { window.clearInterval(timer); window.removeEventListener('hashchange', onHash); window.removeEventListener(authEventName, onUnauthorized); window.removeEventListener('focus', onRefresh); document.removeEventListener('visibilitychange', onRefresh); }; }, []);
  useEffect(() => { if (view !== 'logs') setLogFilter({ q: '', user: 'all', action: 'all', scope: 'all' }); }, [view]);
  useEffect(() => { if (!selectedRecord) return; if (recordProjectEdit.recordId !== selectedRecord.id) { setRecordProjectCategory(projectCategory(selectedRecord)); setRecordProjectEdit({ recordId: selectedRecord.id, token: selectedRecord.edit_token || '' }); } }, [selectedRecord, recordProjectEdit.recordId]);
  useEffect(() => { if (!ready) return; if (view === 'users' && !canManageUsers) setView('dashboard'); if (view === 'upload' && !canAnalyze) setView('dashboard'); if (view === 'logs' && !canAudit) setView('dashboard'); }, [ready, view, canManageUsers, canAnalyze, canAudit]);
  useEffect(() => { if (!ready || !canAnalyze || autoRestoreChecked.current) return; autoRestoreChecked.current = true; const draft = state.batches.find(batch => batch.status === 'review' || batch.status === 'analyzed'); if (draft && !uploadBatchId) void restoreBatch(draft.batch_id || draft.id || '').catch(fail); }, [ready, canAnalyze, state.batches, uploadBatchId]);
  useEffect(() => {
    if (!ready) return;
    const allowedBatchIds = new Set(state.batches.map(batch => batch.batch_id || batch.id).filter(Boolean));
    const hasUploadDraft = Boolean(uploadBatchId || pendingUploadFiles.length || currentUploadId || uploadStage !== 'select' || uploadProgress);
    if (!canAnalyze) {
      if (hasUploadDraft) resetUploadDraft(false);
    } else if (!busy && uploadBatchId && !allowedBatchIds.has(uploadBatchId) && batchAccessCheckRef.current !== uploadBatchId) {
      const batchId = uploadBatchId;
      batchAccessCheckRef.current = batchId;
      api<{ batch_id?: string; id?: string; items: BatchItem[] }>(`/api/batches/${encodeURIComponent(batchId)}`).then(batch => {
        if (uploadBatchIdRef.current !== batchId) return;
        setUploadBatchId(batch.batch_id || batch.id || batchId);
        setPendingUploadFiles(batch.items || []);
        setUploadStage('review');
        setUploadProgress(100);
      }).catch(error => {
        if (uploadBatchIdRef.current !== batchId) return;
        if (isApiError(error) && [403, 404].includes(error.status)) resetUploadDraft(false);
        else fail(error);
      }).finally(() => {
        if (batchAccessCheckRef.current === batchId) batchAccessCheckRef.current = '';
      });
    } else if (!canReview) {
      setCurrentUploadId(null);
      setManualEditToken('');
    }
    if (!canNotify) setNotificationOpen(false);
    if (!canResolve) setExceptionId(null);
    if (!canRead) {
      setDrawerRecordId(null);
      setDrawerFileId(null);
      setSelectedFileId(null);
      return;
    }
    if (drawerRecordId && !records.some(record => record.id === drawerRecordId)) {
      setDrawerRecordId(null);
      setDrawerFileId(null);
      setRecordProjectEdit({ recordId: '', token: '' });
    }
    if (drawerFileId && !files.some(file => file.id === drawerFileId)) setDrawerFileId(null);
    if (selectedFileId && !files.some(file => file.id === selectedFileId)) setSelectedFileId(null);
  }, [ready, busy, canAnalyze, canReview, canNotify, canResolve, canRead, state.batches, uploadBatchId, pendingUploadFiles.length, currentUploadId, uploadStage, uploadProgress, drawerRecordId, drawerFileId, selectedFileId, records, files]);
  useEffect(() => { window.__DMR_TEST_STATE__ = { framework: 'react', busy, uploadStage, uploadBatchId, currentUser, permissions: [...permissions], records, libraryFiles: files, exceptions: state.exceptions, operationLogs: state.operationLogs, notifications: state.notifications }; }, [busy, uploadStage, uploadBatchId, currentUser, permissions, records, files, state.exceptions, state.operationLogs, state.notifications]);
  useEffect(() => { activeUserRef.current = currentUser || authUser; }, [currentUser, authUser]);
  useEffect(() => { uploadBatchIdRef.current = uploadBatchId; }, [uploadBatchId]);

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        setDrawerRecordId(null); setDrawerFileId(null); setRecordProjectEdit({ recordId: '', token: '' }); setNotificationOpen(false); setExceptionId(null); setCurrentUploadId(null); setManualEditToken(''); setPasswordPanelOpen(false); setSidebarOpen(false);
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault(); document.getElementById('topSearch')?.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  const submitNasReadForm = (readBaseUrl: string, ticket: string) => { const form = document.createElement('form'); form.method = 'POST'; form.action = `${readBaseUrl.replace(/\/$/, '')}/read`; form.target = '_blank'; form.setAttribute('rel', 'noopener noreferrer'); form.style.display = 'none'; const input = document.createElement('input'); input.type = 'hidden'; input.name = 'ticket'; input.value = ticket; form.appendChild(input); document.body.appendChild(form); form.submit(); window.setTimeout(() => form.remove(), 1000); };
  const openNasRead = async (id: string, action: 'preview' | 'download') => { const ticket = await api<{ mode: string; ticket?: string; read_base_url?: string; reason?: string }>(`/api/nas/files/${encodeURIComponent(id)}/read-ticket`, { action }); if (ticket.mode !== 'nas' || !ticket.ticket || !ticket.read_base_url) throw new Error(ticket.reason || 'NAS 读取授权不可用，不能绕过个人 192 权限改用本地副本。'); submitNasReadForm(ticket.read_base_url, ticket.ticket); };
  const previewFile = async (id?: string) => { if (!id || !canPreview) return; try { const file = files.find(item => item.id === id); if (!file) throw new Error('文件不存在'); if (file.sync_status === 'synced' || file.storage_mode === 'nas') await openNasRead(id, 'preview'); else window.open(`/api/preview/${encodeURIComponent(id)}`, '_blank', 'noopener'); } catch (error) { fail(error); } };
  const downloadFile = async (id?: string) => { if (!id || !canDownload) return; try { const file = files.find(item => item.id === id); if (!file) throw new Error('文件不存在'); if (file.sync_status === 'synced' || file.storage_mode === 'nas') { await openNasRead(id, 'download'); await loadState(); return; } const response = await fetch(`/api/download/${encodeURIComponent(id)}`, { credentials: 'same-origin' }); if (response.status === 401) window.dispatchEvent(new CustomEvent(authEventName)); if (!response.ok) throw new Error((await response.json()).error || '附件不可用'); const url = URL.createObjectURL(await response.blob()); const link = document.createElement('a'); link.href = url; link.download = file.name; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); await loadState(); } catch (error) { fail(error); } };
  const runSync = async () => { if (busy || !canSync) return; setBusy(true); setSyncOpen(true); try { await api('/api/sync', {}); await loadState(); if (canAudit) setView('logs'); showToast('本地检查完成', '结果已写入日志；外部系统未接入。'); } catch (error) { fail(error); } finally { setBusy(false); setSyncOpen(false); } };
  const analyzeUploadFiles = async (uploadFiles: File[]) => {
    if (!canAnalyze) return;
    if (busy) return showToast('正在处理', '请等待当前操作完成。');
    if (!uploadFiles.length) return;
    if (uploadFiles.length > 200) return showToast('文件数量过多', '单次最多 200 份，请分批上传。');
    setBusy(true);
    setTransferResults([]);
    setUploadStage('processing');
    setUploadProgress(0);
    setUploadBatchId('');
    setPendingUploadFiles(uploadFiles.map(file => ({ id: file.name, batch_id: '', fileName: file.name, status: 'reading', sha256: '', size: file.size })));
    try {
      const total = uploadFiles.reduce((sum, file) => sum + file.size, 0);
      if (total > 55 * 1024 * 1024) throw new Error('单批文件总量不能超过 55 MB，请分批上传。');
      const ordinary: Array<{ name: string; data: string }> = [];
      let needsReview = 0;
      let identicalNames = 0;
      let read = 0;
      for (const file of uploadFiles) {
        if (file.size > 32 * 1024 * 1024) throw new Error(`${file.name} 超过单文件 32 MB 限制`);
        const data = await readAsBase64(file);
        if (isTransferManifest(file.name)) {
          try {
            const archived = await api<Omit<TransferManifestResult, 'filename'>>('/api/transfer-manifests/upload', { name: file.name, data });
            if (archived.already_exists) identicalNames += 1;
            setTransferResults(values => [...values, { ...archived, filename: file.name }]);
          } catch (error) {
            const payload = isApiError(error) && error.status === 409 ? error.payload as Partial<TransferManifestResult> : null;
            if (!payload || payload.archive_status !== 'needs_review' || !payload.network_path || !payload.receipt_id) throw error;
            needsReview += 1;
            setTransferResults(values => [...values, { receipt_id: payload.receipt_id!, archive_status: 'needs_review', network_path: payload.network_path!, file_date: payload.file_date || '', filename: file.name }]);
          }
        } else {
          ordinary.push({ name: file.name, data });
        }
        read += file.size;
        setUploadProgress(Math.round(read / Math.max(total, 1) * 30));
      }
      if (!ordinary.length) {
        setPendingUploadFiles([]);
        setUploadStage('select');
        setUploadProgress(100);
        await loadState();
        showToast(needsReview || identicalNames ? '请核对同名转移清单' : '转移清单已归档', needsReview ? `${needsReview} 份与 192 同名文件内容不同，未覆盖；请查看下方位置。` : identicalNames ? `${identicalNames} 份同名且内容相同，未重复写入；请查看下方位置。` : '已按文件名日期存入 192，位置显示在下方。');
        return;
      }
      const result = await api<{ batch_id: string; items: BatchItem[] }>('/api/analyze', { files: ordinary, restrict_new_subject: true });
      await loadState();
      setUploadBatchId(result.batch_id);
      setPendingUploadFiles(result.items || []);
      setUploadStage('review');
      setUploadProgress(100);
      showToast('真实文件解析完成', needsReview || identicalNames ? '普通文件待核对；转移清单中有同名文件，请查看归档结果。' : '请核对普通文件逐项结果；转移清单已单独归档。');
    } catch (error) {
      setUploadStage('select');
      setPendingUploadFiles([]);
      fail(error);
    } finally {
      setBusy(false);
    }
  };
  const saveUploadReview = async (pending = false, allowIncomplete = false) => { const item = currentUploadItem; if (!item || busy || !canReview) return; const body: Record<string, string | boolean> = { restrict_new_subject: true, save_pending: pending, allow_incomplete: allowIncomplete, confirm_same_version_revision: sameVersionRevision, review_note: manualNote, batch_id: uploadBatchId, item_id: item.id, version: manualVersion.trim(), project_category: manualProjectCategory, expected_edit_token: manualEditToken }; if (manualChoice.startsWith('record:')) body.record_id = manualChoice.slice(7); else if (manualChoice.startsWith('context:')) body.context_record_id = manualChoice.slice(8); else ['code', 'registrant', 'product', 'module', 'type', 'linked_bom'].forEach(key => { body[key] = key in manualFields ? manualFields[key].trim() : String((item as unknown as Record<string, unknown>)[key] || ''); }); setBusy(true); try { await api('/api/review', body); await restoreBatch(uploadBatchId); await loadState(); setCurrentUploadId(null); setManualEditToken(''); showToast(pending ? '已保存待补充' : '人工核对已保存', pending ? '资料继续保留在待核对列表，不会归档。' : '已重新校验，未通过的资料继续待核对。'); } catch (error) { fail(error); } finally { setBusy(false); } };
  const saveProjectCategory = async () => { const record = selectedRecord; if (!record || busy || !canClassify) return; setBusy(true); try { await api(`/api/records/${encodeURIComponent(record.id)}/classification`, { project_category: recordProjectCategory, expected_edit_token: recordProjectEdit.token }); const nextState = await loadState(); const refreshed = nextState.records.find(item => item.id === record.id); setRecordProjectEdit({ recordId: record.id, token: refreshed?.edit_token || '' }); showToast('项目类别已保存', '同一注册人主体和产品的已归档记录已同步更新。'); } catch (error) { fail(error); } finally { setBusy(false); } };
  const commitBatch = async () => {
    if (busy || !uploadBatchId || !canCommit) return;
    setBusy(true);
    try {
      const result = await api<{ committed: number; results?: Array<{ status: string; file_id?: string }>; manual_updates?: string[] }>('/api/commit', { batch_id: uploadBatchId, initial_latest_only: initialLatestOnly, restrict_new_subject: true });
      const nextState = await loadState();
      const archivedFiles = (result.results || []).filter(item => item.status === 'committed' && item.file_id).flatMap(item => {
        const file = nextState.libraryFiles.find(entry => entry.id === item.file_id);
        const record = nextState.records.find(entry => entry.id === file?.recordId);
        return file && record ? [{ id: file.id, name: file.name, location: [record.registrant, projectCategory(record), record.product, record.module].join(' / ') }] : [];
      });
      setLastUploadResult({ matched: result.committed || 0, skipped: (result.results || []).filter(item => item.status !== 'committed').length, manualUpdates: result.manual_updates?.length || 0, archivedFiles });
      setUploadStage('complete');
      showToast('批次处理完成', '真实归档结果及例外项已写入日志。');
    } catch (error) { fail(error); } finally { setBusy(false); }
  };
  const resolveException = async () => { const item = selectedException; if (!item || !canResolve) return; try { if (item.itemId && item.batchId) { const items = await restoreBatch(item.batchId); setExceptionId(null); setView('upload'); const uploadItem = items.find(row => row.id === item.itemId); if (uploadItem) openUploadReview(uploadItem); } else { await api(`/api/exceptions/${encodeURIComponent(item.id)}/resolve`, {}); await loadState(); setExceptionId(null); showToast('异常核对完成', '已按实际验证结果更新。'); } } catch (error) { fail(error); } };

  const handleFinderSearch = (queryOverride?: string) => {
    setCatalogSearch({ ...(queryOverride === undefined ? finder : { registrant: 'all', projectCategory: 'all', product: 'all', module: 'all', query: queryOverride }), revision: Date.now() });
    setView('register');

  };
  const logBadgeCount = openExceptions.length + (state.latestConfirmed ? 0 : 1);

  if (!authReady) {
    return <div className="login-shell"><section className="login-card"><div className="login-loading">正在检查登录状态...</div></section></div>;
  }

  if (!authUser) {
    return <LoginGate notice={authNotice} onAuthenticated={user => { setAuthNotice(''); void bootstrap(user).catch(error => { fail(error); clearAuth(); }); }} />;
  }

  if (authUser.must_change_password) {
    return <PasswordChangeGate user={authUser} forced onChanged={handlePasswordChanged} />;
  }

  return <div className="app-shell">
    <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`} aria-label="主导航">
      <div className="brand"><div className="brand-seal">DMR</div><div><strong>量产文件管理</strong><small>CONTROLLED RECORDS</small></div></div>
      <nav className="nav-list">
        <button className={`nav-item ${view === 'dashboard' ? 'active' : ''}`} data-view="dashboard" onClick={() => setView('dashboard')}><svg viewBox="0 0 24 24"><path d="M4 4h6v6H4zM14 4h6v10h-6zM4 14h6v6H4zM14 18h6v2h-6z" /></svg><span>工作台</span></button>
        <button className={`nav-item ${view === 'register' ? 'active' : ''}`} data-view="register" onClick={() => setView('register')}><svg viewBox="0 0 24 24"><path d="M5 3h14v18H5zM8 7h8M8 11h8M8 15h5" /></svg><span>DMR 清单</span></button>
        <button className={`nav-item ${view === 'files' ? 'active' : ''}`} data-view="files" onClick={() => setView('files')}><svg viewBox="0 0 24 24"><path d="M3 7h7l2 2h9v11H3zM3 7V4h7l2 3" /></svg><span>受控文件</span></button>
        {canAnalyze && <button className={`nav-item ${view === 'upload' ? 'active' : ''}`} data-view="upload" onClick={() => setView('upload')}><svg viewBox="0 0 24 24"><path d="M12 4v11M7 9l5-5 5 5M5 20h14M4 17h16" /></svg><span>批量录入</span><em id="uploadPendingCount" hidden={uploadStage !== 'review' || !pendingUploadFiles.some(item => !['matched','duplicate','committed','skipped'].includes(item.status))}>{pendingUploadFiles.filter(item => !['matched','duplicate','committed','skipped'].includes(item.status)).length || ''}</em></button>}
        {canAudit && <button className={`nav-item ${view === 'logs' ? 'active' : ''}`} data-view="logs" onClick={() => setView('logs')}><svg viewBox="0 0 24 24"><path d="M6 3h12v18H6zM9 7h6M9 11h6M9 15h4M4 6h2M4 10h2M4 14h2" /></svg><span>日志记录</span><em id="logPendingCount" hidden={!logBadgeCount}>{logBadgeCount || ''}</em></button>}
        {canManageUsers && <button className={`nav-item ${view === 'users' ? 'active' : ''}`} data-view="users" onClick={() => setView('users')}><svg viewBox="0 0 24 24"><path d="M16 11a4 4 0 1 0-8 0M4 20a8 8 0 0 1 16 0M19 5v6M16 8h6" /></svg><span>人员与权限</span></button>}
      </nav>
      <div className="sidebar-state"><span className="live-dot" /><div><strong>本地资料库</strong><small>真实文件 · 本机存储</small></div></div>
      <div className="prototype-mark"><span>HTML</span><div><strong>本地运行</strong><small>原版界面 · 本地服务</small></div></div>
    </aside>
    <main className="workspace">
      <div style={{ padding: '12px 24px', background: '#f5ecd9', color: '#614519', fontSize: 13, lineHeight: 1.6 }}><strong>本地运行版 · 2026-09-20 整理</strong>：真实文件保存在本机项目数据目录；依据原件解析并执行版本门禁。飞书清单目录已关联，新版归档后提醒文控手动更新清单；本地归档不代表已完成外部审批。</div>
      <header className="topbar">
        <button className="mobile-menu" id="mobileMenu" aria-label="打开导航" onClick={() => setSidebarOpen(true)}><span /><span /><span /></button>
        <div className="breadcrumb"><span>法规注册部</span><i>/</i><strong id="crumbTitle">{viewLabels[view]}</strong></div>
        <div className="top-actions">
          <label className="top-search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /></svg><input id="topSearch" type="search" placeholder="搜索文件编号或文件名称" onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => { if (event.key === 'Enter' && event.currentTarget.value.trim()) { handleFinderSearch(event.currentTarget.value); } }} /><kbd>⌘ K</kbd></label>
          {canNotify && <button className="icon-button" id="notificationButton" aria-label="变更提醒" onClick={() => setNotificationOpen(true)}><svg viewBox="0 0 24 24"><path d="M6 17h12l-1.5-2v-4a4.5 4.5 0 0 0-9 0v4zM10 20h4" /></svg><span id="notificationCount" className={state.notifications.some(item => !item.read) ? '' : 'empty'}>{state.notifications.filter(item => !item.read).length || ''}</span></button>}
          <button className="text-button top-password-button" id="changePasswordButton" onClick={() => setPasswordPanelOpen(true)}>改密码</button>
          <button className="user-avatar" id="userAvatar" title={`${currentUser?.display_name || '本地用户'} · 点击退出`} onClick={async () => { try { await api('/api/auth/logout', {}); } finally { clearAuth(); } }}>{currentUser?.display_name?.slice(0, 1) || '本'}</button>
        </div>
      </header>
      <div className="view-frame">
        <Dashboard active={view === 'dashboard'} records={records} files={files} exceptions={openExceptions} todayUpdateCount={todayUpdateCount} finder={finder} setFinder={setFinder} onSearch={handleFinderSearch} openRecord={openRecord} setView={setView} runSync={runSync} canSync={canSync} setExceptionId={setExceptionId} />
        <Register catalogSearch={catalogSearch} clearCatalogSearch={() => setCatalogSearch(null)} active={view === 'register'} records={records} files={files} filtered={registerRows} filter={registerFilter} setFilter={setRegisterFilter} openRecord={openRecord} runSync={runSync} canSync={canSync} todayUpdateCount={todayUpdateCount} />
        <FilesView key={fileSearchRevision} searchContext={Object.values(fileScope).filter(value => value !== 'all').join(' / ')} clearSearchScope={() => { setFileScope({ registrant: 'all', projectCategory: 'all', product: 'all', module: 'all' }); setSelectedFileId(null); }} active={view === 'files'} records={records} allFiles={files} files={visibleFiles} fileSearch={fileSearch} setFileSearch={setFileSearch} folderKey={folderKey} setFolderKey={(key: string) => { setFolderKey(key); setFileScope({ registrant: 'all', projectCategory: 'all', product: 'all', module: 'all' }); setSelectedFileId(null); }} selectedFile={selectedFile} selectedFileId={selectedFileId} setSelectedFileId={setSelectedFileId} folderParts={folderParts} openFile={openFile} downloadFile={downloadFile} previewFile={previewFile} canDownload={canDownload} canPreview={canPreview} />
        <Upload initialLatestOnly={initialLatestOnly} setInitialLatestOnly={setInitialLatestOnly} active={view === 'upload'} stage={uploadStage} progress={uploadProgress} batchId={uploadBatchId} pending={pendingUploadFiles} transferResults={transferResults} busy={busy} analyzeUploadFiles={analyzeUploadFiles} restoreLatestBatch={async () => { try { const batch = state.batches[0]; if (!batch) return showToast('暂无历史批次', '先选择真实文件建立录入批次。'); await restoreBatch(batch.batch_id || batch.id || ''); } catch (error) { fail(error); } }} restart={() => { setUploadStage('select'); setUploadBatchId(''); setPendingUploadFiles([]); setTransferResults([]); }} openUploadReview={openUploadReview} commitBatch={commitBatch} lastResult={lastUploadResult} openArchivedFile={openArchivedFile} setView={setView} canAnalyze={canAnalyze} canReview={canReview} canCommit={canCommit} canAudit={canAudit} />
        <Logs active={view === 'logs'} logs={visibleLogs} allLogs={state.operationLogs} exceptions={openExceptions} filter={logFilter} setFilter={setLogFilter} changes={state.latestSyncChanges} latest={{ time: state.latestSyncTime, batch: state.latestSyncBatch, confirmed: state.latestConfirmed, by: state.latestConfirmedBy }} confirmLatestSync={async () => { if (!canSync) return; try { await api('/api/sync/confirm', {}); await loadState(); showToast('已记录本地核对', '此操作不代表外部审批。'); } catch (error) { fail(error); } }} runSync={runSync} canSync={canSync} />
        {canManageUsers && <Users active={view === 'users'} onChanged={async () => { await loadState(); }} />}
      </div>
    </main>
        <Drawers sameVersionRevision={sameVersionRevision} setSameVersionRevision={setSameVersionRevision} archiveSubjects={archiveSubjects} canShare={canCommit && canDownload} shareFile={file => setShareFileId(file.id)} canCorrectArchive={canCorrectArchive} correctArchive={file => setCorrectionFileId(file.id)} manualFields={manualFields} setManualFields={setManualFields} manualNote={manualNote} setManualNote={setManualNote} uploadBatchId={uploadBatchId} record={shareFile ? undefined : selectedRecord} file={selectedDrawerFile} files={files.filter(file => file.recordId === selectedRecord?.id)} notifications={canNotify ? state.notifications : []} notificationOpen={notificationOpen && canNotify} setNotificationOpen={setNotificationOpen} markNotificationsRead={async () => { if (!canNotify) return; try { await api('/api/notifications/read', {}); await loadState(); } catch (error) { fail(error); } }} uploadItem={currentUploadItem} records={records} manualChoice={manualChoice} manualVersion={manualVersion} manualProjectCategory={manualProjectCategory} recordProjectCategory={recordProjectCategory} setManualChoice={setManualChoice} setManualVersion={setManualVersion} setManualProjectCategory={setManualProjectCategory} setRecordProjectCategory={setRecordProjectCategory} closeUploadReview={() => { setCurrentUploadId(null); setManualEditToken(''); }} saveUploadReview={saveUploadReview} saveProjectCategory={saveProjectCategory} exceptionItem={selectedException} closeException={() => setExceptionId(null)} resolveException={resolveException} closeRecord={() => { setDrawerRecordId(null); setDrawerFileId(null); setRecordProjectEdit({ recordId: '', token: '' }); }} downloadFile={downloadFile} canDownload={canDownload} canPreview={canPreview} canClassify={canClassify} canReview={canReview} canResolve={canResolve} />
    {canCommit && canDownload && shareFile && shareRecord && <ShareFile key={shareFile.id} file={shareFile} record={shareRecord} records={records} onClose={() => setShareFileId(null)} onSaved={async result => { await loadState(); setShareFileId(null); setDrawerRecordId(result.record_id); setDrawerFileId(result.file_id); showToast(result.status === 'skipped' ? '目标已存在相同文件' : '附件已复用归档', '原文件保留，目标归属及版本已核对。'); }} />}
    {canCorrectArchive && correctionFile && correctionRecord && <ArchiveCorrection key={correctionFile.id} file={correctionFile} record={correctionRecord} records={records} onClose={() => setCorrectionFileId(null)} onSaved={async result => { const nextState = await loadState(); setCorrectionFileId(null); setDrawerRecordId(result.record_id); setDrawerFileId(result.file_id); const updatedRecord = nextState.records.find(item => item.id === result.record_id); setRecordProjectCategory(projectCategory(updatedRecord)); setRecordProjectEdit({ recordId: result.record_id, token: updatedRecord?.edit_token || '' }); setFolderKey('all'); setFileScope({ registrant: 'all', projectCategory: 'all', product: 'all', module: 'all' }); setFileSearch(''); setSelectedFileId(result.file_id); showToast('归档已更正', '清单归属和版本状态已更新，更正原因已留痕。'); }} />}
    <div className={`sync-overlay ${syncOpen ? 'open' : ''}`} id="syncOverlay" aria-hidden={!syncOpen}><div className="sync-dialog"><div className="sync-spinner" /><h2>正在同步最新数据</h2><p id="syncMessage">正在核对本地附件 SHA-256 与 BOM 关联...</p><div className="progress-bar"><i id="progressBar" style={{ width: syncOpen ? '60%' : '0%' }} /></div><div><span id="syncStep">本地检查</span><strong id="syncPercent">{syncOpen ? '执行中' : '0%'}</strong></div></div></div>
    {passwordPanelOpen && currentUser && <div className="modal-backdrop open" aria-hidden={false} onClick={event => { if (event.target === event.currentTarget) setPasswordPanelOpen(false); }}><PasswordChangeGate user={currentUser} onChanged={handlePasswordChanged} onCancel={() => setPasswordPanelOpen(false)} /></div>}
    <div className="toast-region" id="toastRegion" aria-live="polite">{toast.map(item => <div className="toast" key={item.id}><i>✓</i><div><strong>{item.title}</strong><span>{item.detail}</span></div></div>)}</div>
    <div className={`sidebar-overlay ${sidebarOpen ? 'open' : ''}`} id="sidebarOverlay" onClick={() => setSidebarOpen(false)} />
  </div>;

}
