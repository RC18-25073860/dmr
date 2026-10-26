import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, apiErrorMessage } from '../api';
import type { DmrRecord, LibraryFile } from '../types';
import { projectCategory } from './shared';

type ShareDetails = { edit_token: string; allowed_subjects: string[]; source_record: DmrRecord; source_file: LibraryFile };
export function ShareFile({ file, record, records, onClose, onSaved }: {
  file: LibraryFile; record: DmrRecord; records: DmrRecord[];
  onClose: () => void; onSaved: (result: { record_id: string; file_id: string; status: string }) => Promise<void>;
}) {
  const [details, setDetails] = useState<ShareDetails | null>(null);
  const [target, setTarget] = useState({ registrant: '', project_category: projectCategory(record), product: record.product });
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const dialog = useRef<HTMLElement>(null);
  useEffect(() => {
    let active = true;
    const prior = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    api<ShareDetails>(`/api/files/${encodeURIComponent(file.id)}/share`).then(result => {
      if (!active) return;
      if (result.source_record.id !== record.id || result.source_file.version !== file.version) {
        setError('来源文件已发生变化，请关闭后刷新并重新选择。'); return;
      }
      setDetails(result);
    }).catch(cause => { if (active) setError(apiErrorMessage(cause)); });
    return () => { active = false; prior?.focus(); };
  }, [file.id]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!details || saving || saved) return;
    setSaving(true); setError('');
    let committed = false;
    try {
      const result = await api<{ record_id: string; file_id: string; status: string }>(`/api/files/${encodeURIComponent(file.id)}/share`, {
        expected_edit_token: details.edit_token, reason: reason.trim(), code: record.no, module: record.module,
        registrant: target.registrant, project_category: target.project_category, product: target.product.trim(),
      });
      committed = true; setSaved(true); await onSaved(result);
    } catch (cause) {
      setError(`${committed ? '已完成复用，但页面刷新失败；请关闭并刷新页面。' : ''}${apiErrorMessage(cause)}`);
    } finally { setSaving(false); }
  };
  const sameTarget = target.registrant === record.registrant && target.product.trim() === record.product;
  return <div className="drawer-backdrop open archive-correction-backdrop" onClick={event => { if (event.target === event.currentTarget && !saving) onClose(); }}>
    <aside ref={dialog} tabIndex={-1} className="record-drawer archive-correction" role="dialog" aria-modal="true" aria-labelledby="shareFileTitle" onKeyDown={event => {
      if (event.key === 'Escape' && !saving) onClose();
      if (event.key === 'Tab') {
        const nodes = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)'));
        const first = nodes[0], last = nodes.at(-1);
        if (event.shiftKey && (document.activeElement === first || document.activeElement === event.currentTarget)) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }}>
      <div className="drawer-head"><div><p className="eyebrow">REUSE ARCHIVED FILE</p><h2 id="shareFileTitle">复用到其他主体 / 机型</h2></div><button type="button" aria-label="关闭复用窗口" disabled={saving} onClick={onClose}>×</button></div>
      <form onSubmit={submit}>
        <div className="drawer-body">
          <section className="record-hero"><h3>{file.name}</h3><p>{record.registrant} / {record.product} / {record.module}</p><strong>{record.no} · {file.version} · {file.status}</strong></section>
          <p className="archive-correction-note">复用已保存的同一份附件，无需重复上传。原归属保留；目标主体的版本独立维护，后续更新需分别确认。</p>
          {error && <div className="form-message" role="alert">{error}</div>}
          {!details && !error && <p role="status">正在核对来源文件与权限…</p>}
          <fieldset disabled={!details || saving || saved} className="archive-correction-fields">
            <div className="upload-review-fields">
              <label>目标主体<select required value={target.registrant} onChange={event => setTarget({ ...target, registrant: event.target.value })}><option value="">请选择主体</option>{details?.allowed_subjects.map(subject => <option key={subject}>{subject}</option>)}</select></label>
              <label>项目类别<select required value={target.project_category} onChange={event => setTarget({ ...target, project_category: event.target.value })}><option value="待确认" disabled>请选择项目类别</option><option>医疗</option><option>家电</option></select></label>
              <label>目标机型<input required maxLength={240} list="share-models" value={target.product} onChange={event => setTarget({ ...target, product: event.target.value })} /><datalist id="share-models">{[...new Set(records.filter(item => item.registrant === target.registrant && projectCategory(item) === target.project_category).map(item => item.product))].map(model => <option key={model}>{model}</option>)}</datalist></label>
            </div>
            {sameTarget && <p className="form-message">请选择其他主体或机型，来源清单已包含此附件。</p>}
            <label className="review-pending-note">复用原因<textarea required maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} placeholder="说明这份文件适用于目标主体或机型的依据" /></label>
          </fieldset>
        </div>
        <div className="drawer-footer"><button type="button" className="secondary-button" disabled={saving} onClick={onClose}>{saved ? '关闭' : '取消'}</button><button type="submit" className="primary-button" disabled={!details || saving || saved || sameTarget || !target.registrant || !target.product.trim() || target.project_category === '待确认' || !reason.trim()}>{saving ? '正在归档…' : saved ? '已完成' : '确认复用并归档'}</button></div>
      </form>
    </aside>
  </div>;
}
