import { FormEvent, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { PublicUser, UserAuditEntry, UserRole, UserScope } from '../types';
import { NasAccountBadge, NasAccountModal, NasDirectoryMappingsPanel, NasStoragePanel } from './NasIntegration';
import { projectCategories } from './shared';

const roleLabels: Record<UserRole, string> = {
  admin: '权限管理员',
  document_control: '文控人员',
  reader: '查阅人员',
  uploader: '录入人员',
};

const blankScope = (): UserScope => ({ registrant: '*', project_category: '*', product: '*' });
const scopeCategoryOptions = ['*', ...projectCategories];
const blankForm = () => ({
  id: '',
  username: '',
  display_name: '',
  role: 'reader' as UserRole,
  active: true,
  scopes: [blankScope()],
  can_download: false,
  can_history: false,
  password: '',
  edit_token: '',
  account_template: 'local' as 'local' | 'external_factory',
});

type UserForm = ReturnType<typeof blankForm>;

function scopeText(user: PublicUser) {
  if (user.role === 'admin') return '全部数据';
  if (!user.scopes?.length) return '无数据范围';
  return user.scopes.map(scope => [scope.registrant, scope.project_category, scope.product].map(value => value || '*').join(' / ')).join('；');
}

function extraPermissionText(user: PublicUser) {
  if (user.role === 'admin') return '下载 / 历史';
  return [user.can_download ? '下载' : '', user.can_history ? '历史' : ''].filter(Boolean).join(' / ') || '仅查阅';
}

const fieldLabels: Record<string, string> = {
  username: '账号',
  display_name: '姓名',
  role: '角色',
  active: '启用状态',
  scopes: '数据范围',
  can_download: '下载权限',
  can_history: '历史权限',
  password: '密码',
  identity_source: '账号来源',
  result: '结果',
  parent_path: '父目录',
  folder_name: '新目录名称',
  nas_path: '192 目录',
  account: '192 账号',
  success: '操作成功',
  error: '失败原因',
  job_id: '任务编号',
};

const actionLabels: Record<string, string> = {
  setup: '初始化管理员',
  login: '登录',
  create_user: '新增人员',
  update_user: '修改权限',
  request_nas_directory_creation: '申请新建192目录',
  nas_directory_creation_result: '192目录创建结果',
  save_nas_mapping: '保存192目录映射',
};

const identityLabels: Record<string, string> = {
  local: '本地账号',
  success: '成功',
};

const actionText = (action: string) => actionLabels[action] || action;
const isExplicitScopeRow = (scope: UserScope) => [scope.registrant, scope.project_category, scope.product].some(value => value && value !== '*');
const hasOnlyExplicitScopeRows = (scopes: UserScope[]) => scopes.length > 0 && scopes.every(isExplicitScopeRow);

function formatTime(value: string) {
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

function valueText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '空';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'string') return roleLabels[value as UserRole] || identityLabels[value] || value;
  if (typeof value === 'number') return String(value);
  if (Array.isArray(value)) {
    if (value.every(item => item && typeof item === 'object' && 'registrant' in item && 'project_category' in item && 'product' in item)) {
      return (value as UserScope[]).map(scope => [scope.registrant || '*', scope.project_category || '*', scope.product || '*'].join(' / ')).join('；') || '无';
    }
    return value.map(valueText).join('，');
  }
  try { return JSON.stringify(value); } catch { return String(value); }
}

function nestedText(data: Record<string, unknown>, key: string) {
  const value = data[key];
  return value && typeof value === 'object' ? value as Record<string, unknown> : undefined;
}

function targetText(data: Record<string, unknown>, fallbackId: string) {
  const target = nestedText(data, 'target');
  return valueText(
    target?.display_name ||
    target?.username ||
    data.target_display_name ||
    data.target_name ||
    data.target_username ||
    data.username ||
    data.display_name ||
    fallbackId
  );
}

function auditDetailText(entry: UserAuditEntry) {
  const detail = entry.detail;
  if (typeof detail === 'string') return detail;
  if (!detail || typeof detail !== 'object') return valueText(detail);
  const data: Record<string, unknown> = { ...(detail as Record<string, unknown>), target: entry.target };
  const target = targetText(data, entry.target_id);
  const before = data.before && typeof data.before === 'object' ? data.before as Record<string, unknown> : undefined;
  const after = data.after && typeof data.after === 'object' ? data.after as Record<string, unknown> : undefined;
  const changes = data.changes && typeof data.changes === 'object' ? data.changes as Record<string, unknown> : undefined;
  const parts: string[] = [];
  if (target && target !== '空') parts.push(`对象：${target}`);
  if (before && after) {
    const keys = Array.from(new Set([...Object.keys(before), ...Object.keys(after)])).filter(key => JSON.stringify(before[key]) !== JSON.stringify(after[key]));
    parts.push(...keys.map(key => `${fieldLabels[key] || key}：${valueText(before[key])} → ${valueText(after[key])}`));
  } else if (changes) {
    parts.push(...Object.entries(changes).map(([key, value]) => `${fieldLabels[key] || key}：${valueText(value)}`));
  } else {
    parts.push(...Object.entries(data).filter(([key]) => !key.startsWith('target_') && key !== 'target').map(([key, value]) => `${fieldLabels[key] || key}：${valueText(value)}`));
  }
  return parts.length ? parts.join('；') : valueText(detail);
}

export function Users({ active = true, onChanged }: { active?: boolean; onChanged: () => Promise<void> }) {
  const [users, setUsers] = useState<PublicUser[]>([]);
  const [audit, setAudit] = useState<UserAuditEntry[]>([]);
  const [query, setQuery] = useState('');
  const [editing, setEditing] = useState<UserForm | null>(null);
  const [nasUser, setNasUser] = useState<PublicUser | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  const load = async () => {
    const [userResult, auditResult] = await Promise.all([
      api<{ users: PublicUser[] }>('/api/users'),
      api<{ entries: UserAuditEntry[] }>('/api/users/audit'),
    ]);
    setUsers(userResult.users || []);
    setAudit(auditResult.entries || []);
  };

  useEffect(() => {
    if (active) void load().catch(error => setMessage(error instanceof Error ? error.message : String(error)));
  }, [active]);

  const filtered = useMemo(() => {
    const q = query.toLowerCase();
    return users.filter(user => `${user.username} ${user.display_name} ${roleLabels[user.role] || user.role} ${scopeText(user)}`.toLowerCase().includes(q));
  }, [users, query]);

  const startEdit = (user?: PublicUser) => {
    setMessage('');
    setEditing(user ? {
      id: user.id,
      username: user.username,
      display_name: user.display_name,
      role: user.role,
      active: user.active,
      scopes: user.scopes?.length ? user.scopes.map(scope => ({ registrant: scope.registrant || '*', project_category: scope.project_category || '*', product: scope.product || '*' })) : [],
      can_download: user.can_download,
      can_history: user.can_history,
      password: '',
      edit_token: user.edit_token || '',
      account_template: 'local',
    } : blankForm());
  };

  const startExternalFactory = () => {
    setMessage('');
    setEditing({
      ...blankForm(),
      username: 'factory-',
      role: 'reader',
      scopes: [],
      can_download: false,
      can_history: false,
      account_template: 'external_factory',
    });
  };

  const setScope = (index: number, key: keyof UserScope, value: string) => {
    setEditing(form => form && ({ ...form, scopes: form.scopes.map((scope, scopeIndex) => scopeIndex === index ? { ...scope, [key]: value.trim() || '*' } : scope) }));
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!editing || busy) return;
    if (!editing.username.trim() || !editing.display_name.trim()) {
      setMessage('请填写账号和姓名。');
      return;
    }
    if (!editing.id && !editing.password) {
      setMessage('新增用户需要设置初始密码。');
      return;
    }
    if (editing.account_template === 'external_factory' && !hasOnlyExplicitScopeRows(editing.scopes)) {
      setMessage('外部工厂共用账号的每一条范围都必须明确，不能包含 * / * / * 全量范围。');
      return;
    }
    const scopes = editing.role === 'admin' ? [blankScope()] : editing.scopes.map(scope => ({
      registrant: scope.registrant || '*',
      project_category: scope.project_category || '*',
      product: scope.product || '*',
    }));
    const payload: Record<string, unknown> = {
      display_name: editing.display_name.trim(),
      role: editing.role,
      active: editing.active,
      scopes,
      can_download: editing.can_download,
      can_history: editing.can_history,
    };
    if (!editing.id) payload.username = editing.username.trim();
    if (editing.password) payload.password = editing.password;
    if (editing.id) payload.expected_edit_token = editing.edit_token;
    setBusy(true);
    setMessage('');
    try {
      if (editing.id) await api<{ user: PublicUser }>(`/api/users/${encodeURIComponent(editing.id)}`, payload);
      else await api<{ user: PublicUser }>('/api/users', payload);
      setEditing(null);
      await load();
      await onChanged();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  return <section className={`app-view ${active ? 'active' : ''}`} id="view-users" data-title="人员与权限">
    <div className="page-heading compact">
      <div><p className="eyebrow">LOCAL AUTHORIZATION</p><h1>人员与权限</h1><p>管理本地账号、文控角色、查阅范围、下载和历史权限。企业统一登录未接入，当前账号来源为本地。</p></div>
      <div className="heading-actions"><button className="secondary-button" onClick={startExternalFactory}>外部工厂共用账号</button><button className="primary-button" onClick={() => startEdit()}>新增人员</button></div>
    </div>
    <section className="table-panel permission-panel">
      <div className="nas-management-grid">
        <NasStoragePanel />
        <NasDirectoryMappingsPanel />
      </div>
      <div className="table-toolbar"><label className="table-search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /></svg><input type="search" placeholder="搜索姓名、账号、角色或范围" value={query} onChange={event => setQuery(event.currentTarget.value)} /></label><div><span className="storage-promise">本地身份 · 可迁移接口</span></div></div>
      <div className="table-scroll"><table><thead><tr><th>人员</th><th>角色</th><th>状态</th><th>数据范围</th><th>附加权限</th><th>192 账号</th><th /></tr></thead><tbody>
        {filtered.length ? filtered.map(user => <tr key={user.id}>
          <td><span className="cell-main">{user.display_name}</span><span className="cell-sub">{user.username} · {user.identity_source === 'local' ? '本地账号' : user.identity_source}</span></td>
          <td><span className="module-badge">{roleLabels[user.role] || user.role}</span></td>
          <td><span className={`status-badge ${user.active ? '' : 'error'}`}>{user.active ? '启用' : '停用'}</span></td>
          <td><span className="scope-summary">{scopeText(user)}</span></td>
          <td>{extraPermissionText(user)}</td>
          <td><NasAccountBadge user={user} /></td>
          <td><div className="row-actions"><button className="row-button" onClick={() => setNasUser(user)}>192</button><button className="row-button" onClick={() => startEdit(user)}>编辑</button></div></td>
        </tr>) : <tr><td colSpan={7} className="empty-row">暂无符合条件的人员。</td></tr>}
      </tbody></table></div>
    </section>
    <section className="panel permission-audit">
      <div className="panel-head"><div><p className="eyebrow">AUTHORIZATION AUDIT</p><h2>授权变更记录</h2></div></div>
      <div className="audit-list">{audit.length ? audit.slice(0, 12).map(entry => <article className="audit-item" key={entry.id}><time>{formatTime(entry.time)}</time><strong>{entry.actor_name || entry.actor_id} · {actionText(entry.action)}</strong><p>{auditDetailText(entry)}</p></article>) : <div className="empty-exception"><strong>暂无授权变更</strong><small>新增、停用、改角色和改范围后会记录在这里。</small></div>}</div>
    </section>
    <div className={`modal-backdrop ${editing ? 'open' : ''}`} aria-hidden={!editing} onClick={event => { if (event.target === event.currentTarget) setEditing(null); }}>
      {editing && <form className="permission-modal" role="dialog" aria-modal="true" onSubmit={save}>
        <div className="modal-head"><div><p className="eyebrow">{editing.id ? 'EDIT LOCAL USER' : editing.account_template === 'external_factory' ? 'EXTERNAL FACTORY READER' : 'NEW LOCAL USER'}</p><h2>{editing.id ? '编辑人员权限' : editing.account_template === 'external_factory' ? '新增外部工厂共用账号' : '新增人员'}</h2><p>{editing.account_template === 'external_factory' ? '用于外部工厂查阅已授权 DMR 文件；默认无数据范围、无下载、无历史版本权限。' : '账号用于本地演示，后续可替换为企业身份；这里维护 DMR 业务权限。'}</p></div><button type="button" aria-label="关闭" onClick={() => setEditing(null)}>×</button></div>
        <div className="modal-body permission-form">
          <div className="permission-form-grid">
            <label><span>账号</span><input value={editing.username} disabled={Boolean(editing.id)} onChange={event => { const value = event.currentTarget.value; setEditing(form => form && ({ ...form, username: value })); }} autoComplete="username" /></label>
            <label><span>姓名</span><input value={editing.display_name} onChange={event => { const value = event.currentTarget.value; setEditing(form => form && ({ ...form, display_name: value })); }} /></label>
            <label><span>角色</span><select value={editing.role} disabled={editing.account_template === 'external_factory'} onChange={event => { const value = event.currentTarget.value as UserRole; setEditing(form => form && ({ ...form, role: value })); }}>{Object.entries(roleLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label><span>{editing.id ? '重置密码（可选）' : '初始密码'}</span><input type="password" value={editing.password} onChange={event => { const value = event.currentTarget.value; setEditing(form => form && ({ ...form, password: value })); }} autoComplete="new-password" /></label>
          </div>
          <div className="permission-switches">
            <label><input type="checkbox" checked={editing.active} onChange={event => { const checked = event.currentTarget.checked; setEditing(form => form && ({ ...form, active: checked })); }} />启用账号</label>
            <label><input type="checkbox" checked={editing.role === 'admin' || editing.can_download} disabled={editing.role === 'admin' || editing.account_template === 'external_factory'} onChange={event => { const checked = event.currentTarget.checked; setEditing(form => form && ({ ...form, can_download: checked })); }} />允许下载原件</label>
            <label><input type="checkbox" checked={editing.role === 'admin' || editing.can_history} disabled={editing.role === 'admin' || editing.account_template === 'external_factory'} onChange={event => { const checked = event.currentTarget.checked; setEditing(form => form && ({ ...form, can_history: checked })); }} />允许查看历史版本</label>
          </div>
          <div className="scope-editor">
            <header><strong>数据范围</strong><small>同一行三个条件同时满足；多行之间满足任意一行即可。`*` 表示全部，空范围表示无可见数据。</small></header>
            {editing.role === 'admin' ? <div className="scope-admin-note">权限管理员默认全部数据，并拥有下载、历史和人员维护权限。</div> : editing.scopes.map((scope, index) => <div className="scope-row" key={index}>
              <label><span>注册人主体</span><input value={scope.registrant} onChange={event => setScope(index, 'registrant', event.currentTarget.value)} /></label>
              <label><span>项目类别</span><select value={scope.project_category} onChange={event => setScope(index, 'project_category', event.currentTarget.value)}>{scopeCategoryOptions.map(category => <option key={category} value={category}>{category === '*' ? '* 全部' : category}</option>)}</select></label>
              <label><span>机型</span><input value={scope.product} onChange={event => setScope(index, 'product', event.currentTarget.value)} /></label>
              <button type="button" className="row-button" onClick={() => setEditing(form => form && ({ ...form, scopes: form.scopes.filter((_, scopeIndex) => scopeIndex !== index) }))}>移除</button>
            </div>)}
            {editing.role !== 'admin' && <button type="button" className="secondary-button" onClick={() => setEditing(form => form && ({ ...form, scopes: [...form.scopes, blankScope()] }))}>新增范围行</button>}
          </div>
          {message && <div className="form-message">{message}</div>}
        </div>
        <div className="modal-footer"><span>停用账号会让后端立即拒绝后续请求，历史操作记录保留。</span><div><button type="button" className="secondary-button" onClick={() => setEditing(null)}>取消</button><button className="primary-button" disabled={busy}>{busy ? '保存中...' : '保存权限'}</button></div></div>
      </form>}
    </div>
    <NasAccountModal user={nasUser} open={Boolean(nasUser)} onClose={() => setNasUser(null)} onChanged={async () => { await load(); await onChanged(); }} />
  </section>;
}
