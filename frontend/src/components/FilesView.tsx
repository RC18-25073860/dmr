import { useMemo, useState } from 'react';
import type { DmrRecord, LibraryFile } from '../types';
import { modules, projectCategories, projectCategory, unique } from './shared';

const fileSyncLabel = (file: LibraryFile) => {
  const status = file.sync_status || file.storage_mode || '';
  if (status === 'synced') return '192 已同步';
  if (status === 'pending') return '等待同步';
  if (status === 'transferring') return '同步中';
  if (status === 'blocked') return '同步异常';
  return '本地保存';
};

const fileSyncClass = (file: LibraryFile) => {
  const status = file.sync_status || file.storage_mode || '';
  if (status === 'blocked') return 'error';
  if (status === 'pending' || status === 'transferring') return 'warning';
  return '';
};

const isNasSynced = (file?: LibraryFile) => Boolean(file && (file.sync_status === 'synced' || file.storage_mode === 'nas'));
type VersionFilter = 'all' | 'current' | 'history';

const isHistoryVersion = (file: LibraryFile) => file.status === '历史版本';
const versionStatusLabel = (file: LibraryFile) => isHistoryVersion(file) ? '历史版本' : '当前有效';
const versionMetaText = (file?: LibraryFile) => {
  if (!file?.metadata) return '';
  if (file.metadata.version_rule === 'filename_date') return `按文件名日期判定版本${file.metadata.version_date ? `：${file.metadata.version_date}` : ''}`;
  return [file.metadata.document_version && `原始版本：${file.metadata.document_version}`, file.metadata.version_date && `版本日期：${file.metadata.version_date}`].filter(Boolean).join('；');
};

export function FilesView({
  searchContext,
  clearSearchScope,
  records,
  allFiles,
  files,
  fileSearch,
  setFileSearch,
  folderKey,
  setFolderKey,
  selectedFileId,
  setSelectedFileId,
  folderParts,
  openFile,
  downloadFile,
  previewFile,
  canDownload,
  canPreview,
  active = true,
}: {
  searchContext: string;
  clearSearchScope: () => void;
  records: DmrRecord[];
  allFiles: LibraryFile[];
  files: LibraryFile[];
  fileSearch: string;
  setFileSearch: (value: string) => void;
  folderKey: string;
  setFolderKey: (value: string) => void;
  selectedFile?: LibraryFile;
  selectedFileId: string | null;
  setSelectedFileId: (value: string) => void;
  folderParts: string[];
  openFile: (file: LibraryFile) => void;
  downloadFile: (id?: string) => void;
  previewFile: (id?: string) => void;
  canDownload: boolean;
  canPreview: boolean;
  active?: boolean;
}) {
  const [expandedFolders, setExpandedFolders] = useState<Record<string, boolean>>({});
  const [versionFilter, setVersionFilter] = useState<VersionFilter>('all');
  const isExpanded = (parts: string[]) => expandedFolders[JSON.stringify(parts)] ?? (parts.length === 1 || (parts.length < folderParts.length && parts.every((part, index) => folderParts[index] === part)));
  const visibleFiles = useMemo(() => files.filter(file => versionFilter === 'all' || (versionFilter === 'history' ? isHistoryVersion(file) : !isHistoryVersion(file))), [files, versionFilter]);
  const branchKeys: string[] = [];
  const count = (rs: DmrRecord[]) => {
    const ids = new Set(rs.map(record => record.id));
    return allFiles.filter(file => ids.has(file.recordId)).length;
  };

  const folderNode = (parts: string[], label: string, depth: number, total: number, expandable = false) => {
    const key = parts.length ? JSON.stringify(parts) : 'all';
    const open = expandable && isExpanded(parts);
    return (
      <div className={`folder-row depth-${depth} ${folderKey === key ? 'active' : ''}`} key={key}>
        {expandable ? <button className="folder-toggle" aria-label={`${open ? '收起' : '展开'} ${parts.join(' / ')}`} aria-expanded={open} onClick={() => setExpandedFolders(current => ({ ...current, [key]: !open }))}>
          <svg viewBox="0 0 24 24" className={open ? 'expanded' : ''} aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
        </button> : <span className="folder-toggle-placeholder" aria-hidden="true" />}
        <button
          className={`folder-node ${folderKey === key ? 'active' : ''} ${total ? '' : 'is-empty'}`}
          data-folder-key={key}
          title={parts.join(' / ') || label}
          aria-current={folderKey === key ? 'location' : undefined}
          onClick={() => { setFolderKey(key); if (expandable) setExpandedFolders(current => ({ ...current, [key]: true })); }}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h7l2 2h9v11H3z" /></svg>
          <strong>{label}</strong><span>{total}</span>
        </button>
      </div>
    );
  };

  const tree = [folderNode([], '全部文件', 0, allFiles.length)];
  for (const registrant of unique(records.map(record => record.registrant))) {
    const registrantRows = records.filter(record => record.registrant === registrant);
    const subjectParts = [registrant];
    branchKeys.push(JSON.stringify(subjectParts));
    tree.push(folderNode(subjectParts, registrant, 0, count(registrantRows), true));
    for (const category of projectCategories) {
      const categoryRows = registrantRows.filter(record => projectCategory(record) === category);
      const categoryParts = [registrant, category];
      if (categoryRows.length) branchKeys.push(JSON.stringify(categoryParts));
      if (isExpanded(subjectParts)) tree.push(folderNode(categoryParts, category, 1, count(categoryRows), categoryRows.length > 0));
      for (const product of unique(categoryRows.map(record => record.product))) {
        const productRows = categoryRows.filter(record => record.product === product);
        const productParts = [registrant, category, product];
        branchKeys.push(JSON.stringify(productParts));
        const visible = isExpanded(subjectParts) && isExpanded(categoryParts);
        if (visible) tree.push(folderNode(productParts, product, 2, count(productRows), true));
        if (visible && isExpanded(productParts)) {
          for (const moduleName of modules) {
            tree.push(folderNode([...productParts, moduleName], moduleName, 3, count(productRows.filter(record => record.module === moduleName))));
          }
        }
      }
    }
  }

  const inspectedFile = (selectedFileId ? visibleFiles.find(file => file.id === selectedFileId) : undefined) || visibleFiles[0];
  const activeFileId = inspectedFile?.id || '';
  const record = records.find(item => item.id === inspectedFile?.recordId);
  const fileRecord = (file: LibraryFile) => records.find(item => item.id === file.recordId);

  return (
    <section className={`app-view ${active ? 'active' : ''}`} id="view-files" data-title="受控文件">
      <div className="page-heading compact">
        <div>
          <p className="eyebrow">CONTROLLED LIBRARY</p>
          <h1>受控文件</h1>
          <p>文件保存在本机项目数据目录，按主体、项目类别、机型和模块查询与追溯。</p>
        </div>
        <span className="storage-promise">本地存储 · 可追溯</span>
      </div>
      <section className="library-shell">
        <aside className="folder-pane">
          <div className="folder-head">
            <strong>主体—项目类别—机型—模块</strong>
          </div>
          <div className="folder-tree-actions"><button onClick={() => setExpandedFolders(Object.fromEntries(branchKeys.map(key => [key, true])))}>全部展开</button><button onClick={() => setExpandedFolders(Object.fromEntries(branchKeys.map(key => [key, false])))}>全部收起</button></div>
          <div className="folder-tree" id="folderTree" aria-label="受控文件目录">
            {tree}
          </div>
          <div className="storage-card">
            <div>
              <span>本地归档空间</span>
              <strong>正常</strong>
            </div>
            <i>
              <b></b>
            </i>
            <small>原始附件保存在本机</small>
          </div>
        </aside>
        <div className="file-pane">
          {searchContext && <div className="file-pane-head"><span>查询条件：{searchContext}</span><button className="text-button" onClick={clearSearchScope}>清除筛选</button></div>}
          <div className="file-pane-head">
            <div>
              <span id="folderBreadcrumb">{(folderParts.slice(0, -1).join(' / ') || '全部文件') + ' /'}</span>
              <h2 id="currentFolderTitle">{folderParts.at(-1) || '全部文件'}</h2>
              <span>{visibleFiles.length} 个文件 · {versionFilter === 'all' ? '全部版本' : versionFilter === 'current' ? '当前有效' : '历史版本'}</span>
            </div>
            <div className="row-actions" role="group" aria-label="文件筛选和搜索">
              <label>
                <svg viewBox="0 0 24 24">
                  <circle cx="11" cy="11" r="6" />
                  <path d="m16 16 4 4" />
                </svg>
                <input id="fileSearch" type="search" aria-label="搜索此目录文件" placeholder="搜索文件编号或文件名称，可输入多个关键词" value={fileSearch} onChange={event => setFileSearch(event.currentTarget.value)} />
              </label>
              {[
                ['all', '全部'],
                ['current', '当前有效'],
                ['history', '历史版本'],
              ].map(([value, label]) => <button
                key={value}
                type="button"
                className="row-button"
                aria-pressed={versionFilter === value}
                onClick={() => setVersionFilter(value as VersionFilter)}
              >{label}</button>)}
            </div>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>文件名称</th>
                  <th>版本</th>
                  <th>状态</th>
                  <th>受控日期</th>
                  <th>归档来源</th>
                  <th>存储时效</th>
                  <th></th>
                </tr>
              </thead>
              <tbody id="fileTableBody">
                {visibleFiles.length ? (
                  visibleFiles.map(file => {
                    const rowRecord = fileRecord(file);
                    const metaText = versionMetaText(file);
                    return (
                      <tr key={file.id} className={activeFileId === file.id ? 'active' : ''} onClick={() => setSelectedFileId(file.id)}>
                        <td>
                          <span className="cell-main">{file.name}</span>
                          <span className="cell-sub">{file.recordNo} · {file.type} · {rowRecord?.module}</span>
                        </td>
                        <td>
                          <span className="version-badge">{file.version}</span>
                        </td>
                        <td>
                          <span className={`status-badge ${isHistoryVersion(file) ? 'warning' : ''}`}>{versionStatusLabel(file)}</span>
                        </td>
                        <td><span className="cell-main">{file.date}</span>{metaText && <span className="cell-sub">{metaText}</span>}</td>
                        <td>{file.source}</td>
                        <td>
                          <span className={`status-badge ${fileSyncClass(file)}`}>{fileSyncLabel(file)}</span>
                        </td>
                        <td>
                          <button
                            className="row-button"
                            data-file-id={file.id}
                            onClick={event => {
                              event.stopPropagation();
                              setSelectedFileId(file.id);
                              openFile(file);
                            }}
                          >
                            查看
                          </button>
                        </td>
                      </tr>
                    );
                  })
                ) : (
                  <tr>
                    <td colSpan={7} className="empty-row">此目录暂无{versionFilter === 'history' ? '历史版本' : versionFilter === 'current' ? '当前有效' : '归档'}文件。</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
        <aside className="file-inspector" id="fileInspector">
          {inspectedFile && record ? (
            <>
              {canPreview && !isNasSynced(inspectedFile) ? <iframe className="preview-sheet" title="文件预览" sandbox="" src={`/api/preview/${encodeURIComponent(inspectedFile.id)}`} style={{ width: '100%', border: 0 }}></iframe> : <div className="inspector-empty"><strong>{isNasSynced(inspectedFile) ? '已同步到 192' : '无预览权限'}</strong><small>{isNasSynced(inspectedFile) ? '点击预览会通过个人 192 授权读取。' : '请联系权限管理员开通。'}</small>{canPreview && isNasSynced(inspectedFile) && <button className="row-button" onClick={() => previewFile(inspectedFile.id)}>预览</button>}</div>}
              <div className="inspector-title">
                <strong>{inspectedFile.name}</strong>
                <span>{versionStatusLabel(inspectedFile)}</span>
              </div>
              <dl className="meta-list">
                {[
                  ['注册人主体', record.registrant],
                  ['项目类别', projectCategory(record)], ['分类依据', record.project_category_source || '尚无明确依据'],
                  ['机型 / 模块', `${record.product} / ${record.module}`],
                  ['文件类型', record.type],
                  ['文件版本', inspectedFile.version],
                  ['版本状态', versionStatusLabel(inspectedFile)],
                  ['归档日期', inspectedFile.date],
                  ...(versionMetaText(inspectedFile) ? [['日期版本说明', versionMetaText(inspectedFile)]] : []),
                  ['关联 BOM', inspectedFile.linked_bom || '未指定'],
                  ['关联状态', inspectedFile.link_status || '不适用'],
                  ['192 同步', fileSyncLabel(inspectedFile)],
                  ...(inspectedFile.nas_path ? [['192 目录', inspectedFile.nas_path]] : []),
                  ...(inspectedFile.sync_error ? [['同步说明', inspectedFile.sync_error]] : []),
                  ...(inspectedFile.synced_at ? [['同步时间', inspectedFile.synced_at]] : []),
                  ['文件校验', inspectedFile.sha256],
                ].map(([key, value]) => (
                  <div key={key}>
                    <dt>{key}</dt>
                    <dd style={{ overflowWrap: 'anywhere' }}>{value}</dd>
                  </div>
                ))}
                <div>
                  <dt>原始附件</dt>
                  <dd>
                    {canDownload ? <button className="row-button" data-local-download={inspectedFile.id} onClick={() => downloadFile(inspectedFile.id)}>下载</button> : <span>无下载权限</span>}{' '}
                    {canPreview && (isNasSynced(inspectedFile) ? <button className="row-button" onClick={() => previewFile(inspectedFile.id)}>预览</button> : <a href={`/api/preview/${encodeURIComponent(inspectedFile.id)}`} target="_blank" rel="noopener">预览</a>)}
                  </dd>
                </div>
              </dl>
            </>
          ) : (
            <div className="inspector-empty">
              <strong>未选择文件</strong>
              <small>从左侧目录选择归档文件。</small>
            </div>
          )}
        </aside>
      </section>
    </section>
  );
}
