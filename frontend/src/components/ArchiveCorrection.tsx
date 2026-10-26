import { useEffect, useRef, useState } from 'react';
import { api, apiErrorMessage } from '../api';
import type { DmrRecord, LibraryFile } from '../types';
import { modules } from './shared';

export function ArchiveCorrection({ file, record, records, onClose, onSaved }: {
  file: LibraryFile;
  record: DmrRecord;
  records: DmrRecord[];
  onClose: () => void;
  onSaved: (result: { file_id: string; record_id: string }) => Promise<void>;
}) {
  const [mode, setMode] = useState('existing');
  const [targetId, setTargetId] = useState('');
  const [fields, setFields] = useState({ code: record.no, registrant: record.registrant, product: record.product, module: record.module });
  const [reason, setReason] = useState('');
  const [token, setToken] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const errorRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const target = records.find(item => item.id === targetId);

  useEffect(() => {
    let active = true;
    const previousFocus = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    api<{ edit_token: string; source_record_id: string; code: string; registrant: string; product: string; module: string }>(`/api/files/${encodeURIComponent(file.id)}/correction`)
      .then(result => {
        if (!active) return;
        if (result.source_record_id !== record.id || result.code !== record.no || result.registrant !== record.registrant || result.product !== record.product || result.module !== record.module) {
          setError('文件归属已被更新。请关闭窗口并刷新页面后重新核对。');
          return;
        }
        setToken(result.edit_token);
      })
      .catch(cause => { if (active) setError(apiErrorMessage(cause)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; previousFocus?.focus(); };
  }, [file.id]);

  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || loading || saved || !token) return;
    setError('');
    setSaving(true);
    let committed = false;
    try {
      const result = await api<{ file_id: string; record_id: string }>(`/api/files/${encodeURIComponent(file.id)}/correction`, {
        expected_edit_token: token,
        reason: reason.trim(),
        ...(mode === 'existing' ? { target_record_id: targetId } : Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, value.trim()]))),
      });
      committed = true;
      setSaved(true);
      await onSaved(result);
    } catch (cause) {
      setError(committed ? `归档已更正，但列表刷新失败。请关闭窗口后刷新页面。${apiErrorMessage(cause)}` : apiErrorMessage(cause));
    } finally {
      setSaving(false);
    }
  };

  return <div className="drawer-backdrop open archive-correction-backdrop" onClick={event => { if (event.target === event.currentTarget && !saving) onClose(); }}>
    <aside className="record-drawer archive-correction" role="dialog" aria-modal="true" aria-labelledby="archiveCorrectionTitle" tabIndex={-1} ref={dialogRef} onKeyDown={event => {
      if (event.key === 'Escape' && !saving) { event.stopPropagation(); onClose(); }
      if (event.key === 'Tab') {
        const elements = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]'));
        const first = elements[0], last = elements.at(-1);
        if (event.shiftKey && (document.activeElement === first || document.activeElement === event.currentTarget)) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }}>
      <div className="drawer-head"><div><p className="eyebrow">ARCHIVE CORRECTION</p><h2 id="archiveCorrectionTitle">更正归档</h2></div><button type="button" aria-label="关闭更正归档" disabled={saving} onClick={onClose}>×</button></div>
      <form onSubmit={submit} className="archive-correction-form">
        <div className="drawer-body">
          <section className="record-hero"><span>所选附件</span><h3>{file.name}</h3><p>{record.no} · {record.registrant} · {record.product} · {record.module}</p><div className="record-version"><strong>{file.version}</strong><span>{file.status}</span></div></section>
          <p className="archive-correction-note">仅更正这份附件的归属，原文件和版本保持不变。来源和目标清单的当前版、历史版会重新核对；同版冲突会阻止保存。</p>
          {error && <div className="archive-correction-error" role="alert" tabIndex={-1} ref={errorRef}>{error}</div>}
          {loading && <p role="status">正在读取归档信息…</p>}
          <fieldset disabled={loading || saving || saved || !token} className="archive-correction-fields">
            <div className="upload-review-fields">
              <label>更正方式<select id="correctionMode" value={mode} onChange={event => setMode(event.target.value)}><option value="existing">移到已有 DMR 清单项</option><option value="new">修正归属，建立独立清单项</option></select></label>
              {mode === 'existing' ? <label>正确的 DMR 清单项<select id="correctionTarget" required value={targetId} onChange={event => setTargetId(event.target.value)}><option value="">请选择目标清单项</option>{records.filter(item => item.id !== record.id).map(item => <option key={item.id} value={item.id}>{item.no} · {item.registrant} · {item.product} · {item.module}</option>)}</select></label> : <>{([['code', '文件编号／标识'], ['registrant', '公司主体'], ['product', '产品项目']] as const).map(([key, label]) => <label key={key}>{label}<input id={`correction-${key}`} required maxLength={240} value={fields[key]} onChange={event => setFields({ ...fields, [key]: event.target.value })} /></label>)}<label>资料类别<select id="correction-module" required value={fields.module} onChange={event => setFields({ ...fields, module: event.target.value })}>{Array.from(new Set([...modules, record.module])).map(name => <option key={name} value={name}>{name}</option>)}</select></label></>}
            </div>
            {mode === 'existing' && target && <p className="archive-correction-target">将归入：{target.registrant} / {target.product} / {target.module} / {target.no}</p>}
            <label className="review-pending-note">更正原因（必填）<textarea id="correctionReason" required maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} placeholder="例如：误放入其他产品，应归入所选清单项" /></label>
          </fieldset>
          <p className="archive-correction-note">更正前后归属、操作人、时间和原因会保留在日志中。</p>
        </div>
        <div className="drawer-footer"><button className="secondary-button" type="button" disabled={saving} onClick={onClose}>{saved ? '关闭' : '取消'}</button><button className="primary-button" type="submit" disabled={loading || saving || saved || !token || !reason.trim() || (mode === 'existing' ? !targetId : Object.values(fields).some(value => !value.trim()))}>{saving ? '正在保存…' : saved ? '已更正' : '保存归档更正'}</button></div>
      </form>
    </aside>
  </div>;
}
