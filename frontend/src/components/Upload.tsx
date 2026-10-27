import type { ChangeEvent, DragEvent } from 'react';
import { Fragment } from 'react';
import type { BatchItem, UploadStage, ViewName } from '../types';
import { fileType, projectCategory } from './shared';

const steps: UploadStage[] = ['select', 'processing', 'review', 'complete'];
const stepTitles = ['上传文件', '智能匹配', '人工确认', '完成归档'];
const stepSubtitles = ['选择 1–200 份资料', '对应 DMR 与版本', '只处理例外项', '版本与日志留痕'];

const labels: Record<string, string> = {
  matched: '可归档',
  conflict: '本次不覆盖',
  unmatched: '等待人工核对',
  duplicate: '相同原件已存在',
  committed: '已归档',
  rejected: '解析失败',
  skipped: '同批次较低版本已跳过',
  reading: '读取中'
};

export function Upload({
  initialLatestOnly, setInitialLatestOnly,
  active = true,
  stage,
  progress,
  batchId,
  pending,
  transferResults,
  busy,
  analyzeUploadFiles,
  restoreLatestBatch,
  restart,
  openUploadReview,
  commitBatch,
  lastResult,
  openArchivedFile,
  setView,
  canAnalyze,
  canReview,
  canCommit,
  canAudit
}: {
  initialLatestOnly: boolean;
  setInitialLatestOnly: (value: boolean) => void;
  active?: boolean;
  stage: UploadStage;
  progress: number;
  batchId: string;
  pending: BatchItem[];
  transferResults: Array<{ receipt_id: string; archive_status: string; network_path: string; file_date: string; filename: string; already_exists?: boolean }>;
  busy: boolean;
  analyzeUploadFiles: (files: File[]) => void;
  restoreLatestBatch: () => Promise<void>;
  restart: () => void;
  openUploadReview: (item: BatchItem) => void;
  commitBatch: () => void;
  lastResult: { matched: number; skipped: number; manualUpdates?: number; archivedFiles?: Array<{ id: string; name: string; location: string }> };
  openArchivedFile: (id: string) => void;
  setView: (view: ViewName) => void;
  canAnalyze: boolean;
  canReview: boolean;
  canCommit: boolean;
  canAudit: boolean;
}) {
  const stepIndex = steps.indexOf(stage);
  const matched = pending.filter(item => item.status === 'matched').length;
  const duplicate = pending.filter(item => item.status === 'duplicate').length;
  const conflict = pending.filter(item => item.status === 'conflict').length;
  const unmatched = pending.filter(item => item.status === 'unmatched' || item.status === 'rejected').length;
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    analyzeUploadFiles([...event.dataTransfer.files]);
  };

  return <section className={`app-view ${active ? 'active' : ''}`} id="view-upload" data-title="批量录入">
    <div className="page-heading compact"><div><p className="eyebrow">CONTROLLED BATCH INTAKE</p><h1>批量上传录入</h1><p>一次提交一批资料，读取真实文件并执行版本门禁；缺失信息由人工核对。</p></div><span className="storage-promise">批量归档 · 日志留痕</span></div>
    {canAnalyze && <div className="intake-initial-rule"><label><input type="checkbox" checked={initialLatestOnly} disabled={busy || stage === 'complete'} onChange={event => setInitialLatestOnly(event.currentTarget.checked)} /> 首次建立清单时，仅归档同一文件的最新版本</label><p>适用于尚未归档的文件编号，包括日期版本的成品 BOM；已有记录仍保留正常更新和历史追溯。首次导入无需补录旧版或已作废文件。</p></div>}
    {!!transferResults.length && <div className="intake-initial-rule" role="status"><strong>DMR 文件转移清单归档结果</strong><p>按文件名日期确定目标目录；以下逐份显示实际写入或需要人工核对的位置。</p><p><strong>待文控核对 DMR 清单：</strong>转移清单已单独处理，云端清单仍需人工检查和更新。</p><button type="button" className="secondary-button" onClick={() => setView('register')}>查看 DMR 清单</button>{transferResults.map(result => <div className={`transfer-manifest-result ${result.archive_status === 'needs_review' || result.already_exists ? 'needs-review' : ''}`} key={result.receipt_id}><strong>{result.filename}</strong><p>{result.archive_status === 'needs_review' ? '需要人工核对：192 目标位置已有同名但内容不同的文件。原文件未覆盖；新文件暂存在平台，尚未写入 192。' : result.already_exists ? '请人工核对：192 已有同名且内容相同的文件，本次没有重复写入。' : '已写入 192 共享盘。'}{result.file_date ? ` 文件日期：${result.file_date}` : ''}</p><small>{result.archive_status === 'needs_review' ? '冲突的 192 目标位置' : '192 文件位置'}</small><code>{result.network_path}</code><small>平台回执：{result.receipt_id}</small></div>)}</div>}
    {!canAnalyze && <div className="empty-exception"><strong>当前账号没有批量录入权限</strong><small>如需上传和整理资料，请联系权限管理员开通文控权限。</small></div>}

    <div className="intake-stepper" id="intakeStepper" aria-label="录入进度">
      {steps.map((item, index) => <Fragment key={item}>
        <div className={`intake-step ${index === stepIndex ? 'active' : ''} ${index < stepIndex ? 'done' : ''}`} data-upload-step={item}>
          <i>{String(index + 1).padStart(2, '0')}</i><span><strong>{stepTitles[index]}</strong><small>{stepSubtitles[index]}</small></span>
        </div>
        {index < steps.length - 1 && <b />}
      </Fragment>)}
    </div>

    <section className="intake-stage" id="uploadSelectStage" hidden={stage !== 'select' || !canAnalyze}>
      <div className="intake-entry-grid">
        <article className="intake-drop-panel">
          <header className="intake-panel-head"><div><span>BATCH PASSPORT</span><h2>建立录入批次</h2></div><em>统一入口</em></header>
          <div className="transfer-list-meta"><span>文件识别依据</span><strong>BOM 表头 / 软件履历 / 文件名</strong><small>DMR 文件转移清单按文件名日期直接归档到 192，无需补主体、型号、版本等字段；其他文件缺失或冲突时由人工核对。</small></div>
          <div className="upload-dropzone" id="uploadDropzone" onDragOver={event => event.preventDefault()} onDrop={onDrop}>
            <span className="upload-mark"><svg viewBox="0 0 24 24"><path d="M12 4v11M7 9l5-5 5 5M5 20h14" /></svg></span>
            <strong>将一批量产文件放到这里</strong><p>支持 XLSX、PDF、DOCX、HEX、软件包及配置；单次最多 200 份</p>
            <div><label className="primary-button" htmlFor="batchFileInput">选择批量文件</label><input id="batchFileInput" type="file" multiple accept=".pdf,.xlsx,.xls,.doc,.docx,.zip,.hex,.socprj,.v16py32config" onChange={(event: ChangeEvent<HTMLInputElement>) => analyzeUploadFiles([...(event.currentTarget.files || [])])} /><button className="secondary-button" id="loadSampleBatch" onClick={() => void restoreLatestBatch()}>恢复上次录入</button></div>
          </div>
          <div className="intake-format-note"><span>PDF</span><span>XLS/XLSX</span><span>DOC/DOCX</span><span>ZIP</span><small>不支持的文件会单独提示，不影响同批次其他文件</small></div>
        </article>

        <article className="intake-rule-panel">
          <header className="intake-panel-head"><div><span>CONTROL GATES</span><h2>系统会依次完成</h2></div><em>4 道门禁</em></header>
          <ol className="intake-rule-list">
            <li><i>01</i><div><strong>读取清单和文件名</strong><small>提取文件编号、注册人主体、产品、模块和版本。</small></div></li>
            <li><i>02</i><div><strong>对应 DMR 清单</strong><small>按主体、产品、模块，以及文件编号或文件名称查找候选；多份命中时由文控确认。</small></div></li>
            <li><i>03</i><div><strong>执行版本门禁</strong><small>仅高于当前版本时升版；重复、降版和缺失版本不覆盖。</small></div></li>
            <li><i>04</i><div><strong>本地归档并留痕</strong><small>旧版转历史版本，新版归档，同时写入日志和变更提醒。</small></div></li>
          </ol>
          <div className="intake-safety-note"><i>✓</i><span><strong>批次安全原则</strong><small>单个文件冲突不会阻塞其他文件；所有例外项必须由体系文控确认。</small></span></div>
        </article>
      </div>
    </section>

    <section className="intake-stage" id="uploadProcessingStage" hidden={stage !== 'processing' || !canAnalyze}>
      <article className="intake-processing-card">
        <header><div><span id="uploadProcessBatch">{batchId || 'UPLOAD-—'}</span><h2 id="uploadProcessMessage">正在读取文件转移清单</h2><p>上传解析时请保持页面打开；解析后的批次可恢复。</p></div><strong id="uploadProgressPercent">{progress}%</strong></header>
        <div className="intake-progress"><i id="uploadProgressBar" style={{ width: `${progress}%` }} /></div>
        <div className="intake-process-rows" id="uploadProcessRows">
          {pending.map(item => <div className={`intake-process-row ${item.status !== 'reading' ? 'done' : ''}`} key={item.fileName}>
            <i>{item.status !== 'reading' ? '✓' : '…'}</i><div><strong>{item.fileName}</strong><small>读取与解析真实文件；完成后显示逐项结果</small></div><span>{item.status !== 'reading' ? 'DONE' : 'READING'}</span>
          </div>)}
        </div>
      </article>
    </section>

    <section className="intake-stage" id="uploadReviewStage" hidden={stage !== 'review' || !canAnalyze}>
      <article className="intake-review-card">
        <header className="intake-batch-head"><div><span id="uploadBatchMeta">批次 {batchId} · 原件暂存本机，等待确认归档</span><h2>智能匹配完成，确认本次录入</h2><p>自动匹配项已勾选；冲突和未识别项不会覆盖，可逐项人工核对。</p></div><button className="secondary-button" data-action="restart-upload" onClick={restart}>重新选择文件</button></header>
        <div className="analysis-summary" id="analysisSummary"><div className="analysis-metric success"><strong>{matched}</strong><span>可归档</span></div><div className="analysis-metric warning"><strong>{conflict + duplicate}</strong><span>版本冲突 / 重复 {duplicate}</span></div><div className="analysis-metric error"><strong>{unmatched}</strong><span>需人工核对</span></div></div>
        <div className="analysis-table intake-analysis-table"><table><thead><tr><th>上传文件</th><th>系统对应</th><th>版本判断</th><th>处理结果</th><th>依据 / 操作</th></tr></thead><tbody id="uploadAnalysisBody">{pending.map(item => <tr key={item.id}><td><span className="upload-file"><i>{fileType(item.fileName)}</i><strong>{item.fileName}</strong></span>{item.metadata?.email_source?.mail_case === 'bom' && <small>BOM 邮件 · 发放批次</small>}</td><td><span className="match-result"><strong>{item.code || '未识别'}</strong><small>{[projectCategory(item), item.registrant, item.product, item.module].filter(Boolean).join(' · ') || '需补充归属信息'}</small></span></td><td><span className="version-compare"><b>{item.current || '无'}</b><i>→</i><strong>{item.version || item.detected || '缺失'}</strong></span></td><td><span className={`status-badge ${item.status === 'matched' || item.status === 'committed' ? '' : 'warning'}`}>{labels[item.status] || item.status}</span></td><td><span className="upload-row-action"><small>{item.recommendation || (Array.isArray(item.notes) ? item.notes.join('；') : item.notes)}</small>{item.status === 'committed' ? <b>✓ 已归档</b> : canReview ? <button className="row-button" data-upload-review={item.id} onClick={() => openUploadReview(item)}>人工核对</button> : <b>无核对权限</b>}</span></td></tr>)}</tbody></table></div>
        <div className="upload-rule-note"><i>i</i><p><strong>录入规则</strong>：归档新记录与新版；按已确认的日期规则补充旧版时，仅保留为历史。未解决的例外项继续待核对。</p></div>
        <footer className="intake-review-footer"><span id="uploadFooterHint">共 {pending.length} 份：{matched} 份可归档，{duplicate} 份重复跳过，{conflict + unmatched} 份待核对</span>{canCommit && <button className="primary-button" id="confirmBatchUpload" disabled={busy || !(matched + duplicate)} onClick={commitBatch}>{matched ? `确认归档 ${matched} 个文件` : duplicate ? '记录重复并跳过' : '没有可录入文件'}</button>}</footer>
      </article>
    </section>

    <section className="intake-stage" id="uploadCompleteStage" hidden={stage !== 'complete'}>
      <article className="intake-complete-card">
        <span className="intake-complete-mark">✓</span><p className="eyebrow">BATCH ARCHIVED</p><h2 id="uploadCompleteTitle">{lastResult.matched} 份文件已归档到本机</h2><p id="uploadCompleteDetail">批次 {batchId} · {lastResult.skipped} 份重复或例外项未覆盖。完整结果见日志。</p>
        {!!lastResult.archivedFiles?.length && <div className="intake-archived-list"><strong>本次归档文件</strong><ul>{lastResult.archivedFiles.map(file => <li key={file.id}><button type="button" className="secondary-button" onClick={() => openArchivedFile(file.id)}>{file.name}<small>{file.location} · 查看受控文件 ↗</small></button></li>)}</ul></div>}
        {!!lastResult.manualUpdates && <div className="upload-rule-note" role="status"><i>i</i><p>{lastResult.manualUpdates} 条清单更新提醒已生成，请文控打开对应清单手动修改。</p></div>}
        <div className="intake-complete-actions">{ !!lastResult.manualUpdates && <button className="primary-button" onClick={() => setView('register')}>查看待更新清单</button>}<button className="secondary-button" data-action="restart-upload" onClick={restart}>继续录入</button>{canAudit && <button className="primary-button" onClick={() => setView('logs')}>查看本次日志</button>}</div>
      </article>
    </section>
  </section>;
}
