
import type { DmrRecord } from '../types';
export const modules = ['BOM', '工艺文件', '检验文件', '软件', '承认书&规格书', '包装图纸', '结构图纸'];
export const projectCategories = ['医疗', '家电', '待确认'];
export const unique = (values: string[]) => [...new Set(values.filter(Boolean))];
export const brief = (value = '', n = 58) => String(value).length > n ? `${String(value).slice(0, n)}…` : String(value);
export const fileType = (name = '') => (name.split('.').pop()?.toUpperCase() || 'FILE').slice(0, 4);
export const projectCategory = (record?: Pick<DmrRecord, 'project_category'> | { project_category?: string }) => projectCategories.includes(String(record?.project_category || '')) ? String(record?.project_category) : '待确认';
export const projectCategoryClass = (value?: string) => value === '医疗' ? 'medical' : value === '家电' ? 'home' : '';
export function MetricStrip({ id, items }: { id?: string; items: Array<[number | string, string]> }) { return <div className="metric-strip" id={id}>{items.map(([value, label]) => <div className="metric-card" key={label}><i /><strong>{value}</strong><span>{label}</span></div>)}</div>; }
export function SelectFilter({ id, value, label, values, onChange }: { id: string; value: string; label: string; values: string[]; onChange: (value: string) => void }) { return <select id={id} aria-label={label} value={value} onChange={event => onChange(event.currentTarget.value)}><option value="all">{label}</option>{unique(values).map(value => <option key={value}>{value}</option>)}</select>; }
export function RecordRow({ record, openRecord }: { record: DmrRecord; openRecord: (id: string) => void }) { const category = projectCategory(record); return <tr><td><span className="cell-main">{record.no}</span><span className="cell-sub">{record.name}</span></td><td>{record.registrant}</td><td><span className={`type-badge ${projectCategoryClass(category)}`}>{category}</span></td><td>{record.product}</td><td><span className="module-badge">{record.module}</span></td><td><span className="version-badge">{record.version}</span></td><td><span className="file-link">{record.file}</span></td><td>{record.date}</td><td><button className="row-button" data-record={record.id} onClick={() => openRecord(record.id)}>查看</button></td></tr>; }
