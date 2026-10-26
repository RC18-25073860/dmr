import { FormEvent, useEffect, useState } from 'react';
import { api } from '../api';
import type { PublicUser } from '../types';

type Mode = 'checking' | 'setup' | 'login';

export function LoginGate({ onAuthenticated, notice = '' }: { onAuthenticated: (user: PublicUser) => void; notice?: string }) {
  const [mode, setMode] = useState<Mode>('checking');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(notice);
  const [form, setForm] = useState({ display_name: '佳佳', username: 'jiajia', password: '' });

  useEffect(() => {
    let alive = true;
    api<{ setup_required: boolean; mode?: string }>('/api/auth/status')
      .then(status => {
        if (!alive) return;
        setMode(status.setup_required ? 'setup' : 'login');
      })
      .catch(error => {
        if (!alive) return;
        setMessage(error instanceof Error ? error.message : String(error));
        setMode('login');
      });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (notice) setMessage(notice);
  }, [notice]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (!form.username.trim() || !form.password) {
      setMessage('请填写账号和密码。');
      return;
    }
    if (mode === 'setup' && !form.display_name.trim()) {
      setMessage('请填写显示姓名。');
      return;
    }
    setBusy(true);
    setMessage('');
    try {
      const endpoint = mode === 'setup' ? '/api/auth/setup' : '/api/auth/login';
      const payload = mode === 'setup' ? form : { username: form.username, password: form.password };
      const result = await api<{ user: PublicUser }>(endpoint, payload);
      onAuthenticated(result.user);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  return <div className="login-shell">
    <section className="login-card">
      <div className="brand"><div className="brand-seal">DMR</div><div><strong>量产文件管理</strong><small>LOCAL ACCESS</small></div></div>
      <div className="login-body">
        <p className="eyebrow">{mode === 'setup' ? 'FIRST LOCAL ADMIN' : 'LOCAL LOGIN'}</p>
        <h1>{mode === 'setup' ? '设置本地管理员' : '登录本地账号'}</h1>
        <p>当前为本地身份模式，后续可接入企业统一登录；DMR 内部权限仍由本模块维护。</p>
        {mode === 'checking' ? <div className="login-loading">正在检查本地账号状态...</div> : <form className="login-form" onSubmit={submit}>
          {mode === 'setup' && <label><span>显示姓名</span><input value={form.display_name} onChange={event => { const value = event.currentTarget.value; setForm(item => ({ ...item, display_name: value })); }} autoComplete="name" /></label>}
          <label><span>账号</span><input value={form.username} onChange={event => { const value = event.currentTarget.value; setForm(item => ({ ...item, username: value })); }} autoComplete="username" /></label>
          <label><span>{mode === 'setup' ? '设置密码' : '密码'}</span><input type="password" value={form.password} onChange={event => { const value = event.currentTarget.value; setForm(item => ({ ...item, password: value })); }} autoComplete={mode === 'setup' ? 'new-password' : 'current-password'} /></label>
          {message && <div className="form-message">{message}</div>}
          <button className="primary-button" disabled={busy}>{busy ? '请稍候...' : mode === 'setup' ? '创建并进入' : '登录'}</button>
        </form>}
      </div>
    </section>
  </div>;
}
