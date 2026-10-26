import type { FileRuleMetadata } from '../types';

const kindLabels: Record<string, string> = {
  bom: '成品 BOM', bom_notice: 'BOM 类 · 发放通知', software: '软件程序 / 附件', history: '软件履历表', release: '软件发布表',
  structural_drawing: '结构图纸', packaging_drawing: '包装图纸', acknowledgement: '承认书',
  specification: '规格书', incoming_inspection: '来料检验文件', process: '工艺文件',
  inspection: '检验文件', hardware: '硬件图纸', ecn: 'ECN 变更文件',
};
const fieldLabels: Record<string, string> = {
  bom_no: 'BOM 编号', material_no: '物料号', drawing_no: '图号', ecn_no: 'ECN 编号',
  file_no: '文件编号', document_version: '文档版本标记', software_branch: '软件分支',
  software_version: '程序版本', drawing_title: '图纸名称', linked_bom: '关联 BOM',
  version_date: 'BOM 日期版本', drawing_date: '绘图日期', effective_date: '生效日期',
  filename_date: '文件名日期', ecn_month: 'ECN 编号月份', software_history_code: '履历标识',
};

export function FileRuleEvidence({ metadata }: { metadata?: FileRuleMetadata }) {
  if (!metadata?.management_kind && !metadata?.field_evidence?.length) return null;
  const fields = [...Object.entries(metadata.identifiers || {}), ...Object.entries(metadata.dates || {})].filter(([, value]) => value);
  return <section className="upload-rule-note" aria-label="文件识别依据"><i>i</i><div>
    <strong>识别类型：{kindLabels[metadata.management_kind || ''] || '待人工确认'}</strong>
    {metadata.trial_flag && <p>文件含试产信息，请确认适用阶段后再归档。</p>}
    {(metadata.management_kind === 'history' || metadata.management_kind === 'release') && <p>配套资料中的程序版本不代表所有软件附件同时升版，请分别核对。</p>}
    {!!fields.length && <details><summary>查看编号和日期依据 · {fields.length} 项</summary>
      {fields.map(([key, value]) => <p key={key}>{fieldLabels[key] || key}：{value}</p>)}
      <p>以上为解析线索。正文与文件名不一致时，按原件及受控依据人工确认。</p>
    </details>}
  </div></section>;
}
