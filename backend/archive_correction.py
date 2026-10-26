"""Transactional reassignment of an archived attachment, without changing its bytes."""
from functools import cmp_to_key

from fastapi import HTTPException

from backend.editing import edit_token, require_edit_token
from backend.service import clean_text, compare_versions, dated_bom_policy_error, json_dumps, now_iso, sha256_bytes


def snapshot(store, access):
    records = [dict(r) for r in store.execute('SELECT * FROM records ORDER BY id').fetchall() if access.matches(dict(r))]
    ids = {r['id'] for r in records}
    files = [dict(f) for f in store.execute('SELECT * FROM files ORDER BY id').fetchall() if f['record_id'] in ids]
    return {'records': records, 'files': files}


def correction_details(store, access, file_id):
    access.require('correct_archive')
    file = dict(access.file(file_id))
    source = access.record(file['record_id'])
    return {'file_id': file_id, 'source_record_id': source['id'],
            'edit_token': edit_token('archive-correction', file_id, snapshot(store, access)),
            'code': source['business_code'], 'registrant': source['registrant'],
            'product': source['product'], 'module': source['module']}


def ordered_files(files):
    # Validate every pair, including old history, before sorting or writing anything.
    for i, left in enumerate(files):
        if compare_versions(left['version'], left['version']) is None:
            raise HTTPException(409, '存在无法严格比较的版本，请先核对原始版本')
        for right in files[i + 1:]:
            result = compare_versions(left['version'], right['version'])
            if result is None:
                raise HTTPException(409, '目标或来源包含无法严格比较的版本，未保存更正')
            if result == 0:
                raise HTTPException(409, '同版本已有附件，不能通过归档更正覆盖或重复归档')
    return sorted(files, key=cmp_to_key(lambda a, b: -compare_versions(a['version'], b['version'])))


def rebuild(store, record_id, files):
    store.execute("UPDATE files SET status='历史版本' WHERE record_id=?", (record_id,))
    if not files:
        # Keep the shell: old batch references and audit identities remain valid.
        store.execute("UPDATE records SET version='',previous='',file_name='',date='',controlled='',size='',linked_bom='',metadata='{}',edit_revision=edit_revision+1,updated_at=? WHERE id=?", (now_iso(), record_id))
        return
    current = files[0]
    store.execute("UPDATE files SET status='当前有效' WHERE id=?", (current['id'],))
    store.execute('UPDATE records SET version=?,previous=?,file_name=?,date=?,controlled=?,source=?,size=?,linked_bom=?,metadata=?,edit_revision=edit_revision+1,updated_at=? WHERE id=?',
                  (current['version'], files[1]['version'] if len(files)>1 else '', current['name'], current['date'], current['date'], current['source'], current['size'], current['linked_bom'], current['metadata'], now_iso(), record_id))


def correct_archive(store, access, file_id, payload):
    with store.lock, store.conn.immediate():
        details = correction_details(store, access, file_id)
        require_edit_token(payload.get('expected_edit_token'), details['edit_token'])
        reason = clean_text(payload.get('reason'))
        if not reason or len(reason)>2000:
            raise HTTPException(400, '请填写更正原因（1–2000字）')
        file = dict(access.file(file_id))
        source = access.record(file['record_id'])
        try:
            content = store.read_blob(file['blob_path'])
        except (OSError, ValueError) as exc:
            raise HTTPException(409, '本地附件缺失或不可读取，请先恢复原件再更正归档') from exc
        if sha256_bytes(content) != file['sha256']:
            raise HTTPException(409, '本地附件校验失败，请先恢复原件再更正归档')
        before = {'file': file, 'source': source}
        target_id = clean_text(payload.get('target_record_id'))
        if target_id:
            target = access.record(target_id)
            if target_id == source['id']:
                raise HTTPException(400, '请选择不同的归档位置')
        else:
            fields = {key: clean_text(payload.get(key)) for key in ('code', 'registrant', 'product', 'module')}
            if any(not v or v in {'待确认', '未知'} or len(v)>240 for v in fields.values()):
                raise HTTPException(400, '请完整填写文件编号、主体、产品和模块（每项不超过240字）')
            target = source | fields | {'business_code': fields['code']}
            categories = store.execute("SELECT DISTINCT project_category FROM records WHERE registrant=? AND product=? AND project_category IN ('医疗','家电')", (fields['registrant'], fields['product'])).fetchall()
            if len(categories) == 1:
                target['project_category'] = categories[0]['project_category']
                target['project_category_source'] = '继承同主体同产品已确认类别'
            access.require_resource(target)
            existing = store.execute('SELECT id FROM records WHERE business_code=? AND registrant=? AND product=? AND module=?',
                                     (fields['code'], fields['registrant'], fields['product'], fields['module'])).fetchone()
            if existing:
                raise HTTPException(409, '该归属已有清单记录，请选择已有清单项进行更正')
        error = dated_bom_policy_error(file | {'code': target['business_code'], 'module': target['module']})
        if error:
            raise HTTPException(409, error)
        source_files = [dict(r) for r in store.execute('SELECT * FROM files WHERE record_id=? ORDER BY id', (source['id'],)).fetchall()]
        target_files = [dict(r) for r in store.execute('SELECT * FROM files WHERE record_id=? ORDER BY id', (target_id,)).fetchall()] if target_id else []
        # Changing current/previous also manipulates history, not just the selected file.
        if len(source_files)>1 or target_files or file['status'] != '当前有效':
            access.require('history')
        for row in target_files:
            error = dated_bom_policy_error(row | {'code': target['business_code'], 'module': target['module']})
            if error:
                raise HTTPException(409, error)
        remaining = ordered_files([r for r in source_files if r['id'] != file_id])
        incoming = ordered_files(target_files + [file])
        job = store.execute('SELECT * FROM nas_sync_jobs WHERE file_id=?', (file_id,)).fetchone()
        if job and (job['status'] not in ('pending', 'blocked')
                    or job['attempts'] != 0 or job['synced_at']
                    or job['lease_token'] or job['lease_until'] or job['last_heartbeat_at']):
            raise HTTPException(409, '附件已进入192同步流程，不能仅更改平台归属；请先由管理员核对并处理同步位置，再进行归档更正')
        before['nas_sync_job'] = dict(job) if job else None
        before['target'] = dict(target) if target_id else None
        before['versions'] = source_files + target_files
        if not target_id:
            target = dict(store.create_record_shell(target | {'metadata': file['metadata']}, '归档更正'))
            target_id = target['id']
        store.execute('UPDATE files SET record_id=? WHERE id=?', (target_id, file_id))
        store.execute("UPDATE exceptions SET record_id=?,record_no=? WHERE item_id=? AND batch_id=''", (target_id, target['business_code'], file_id))
        if job:
            store.execute("UPDATE nas_sync_jobs SET record_id=?,status='blocked',target_path='',target_name='',next_attempt_at='',last_error='归档已更正，请核对目标目录后重试同步',updated_at=? WHERE file_id=?", (target_id, now_iso(), file_id))
        rebuild(store, source['id'], remaining)
        rebuild(store, target_id, incoming)
        # Re-evaluate links affected by BOM identity changes, without silently writing outside scope.
        for row in store.execute('SELECT files.*,records.registrant,records.product,records.project_category FROM files JOIN records ON records.id=files.record_id').fetchall():
            status = store.link_status(row['linked_bom'], row['registrant'], row['product'], row['project_category'])
            if status != row['link_status']:
                access.require_resource(dict(row))
                if row['status'] != '当前有效': access.require('history')
                store.execute('UPDATE files SET link_status=? WHERE id=?', (status, row['id']))
        after = {'file': dict(store.execute('SELECT * FROM files WHERE id=?', (file_id,)).fetchone()),
                 'source': dict(store.execute('SELECT * FROM records WHERE id=?', (source['id'],)).fetchone()),
                 'target': dict(store.execute('SELECT * FROM records WHERE id=?', (target_id,)).fetchone()),
                 'versions': [dict(row) for row in store.execute('SELECT * FROM files WHERE record_id IN (?,?) ORDER BY id', (source['id'], target_id)).fetchall()]}
        store.add_log('归档更正', file_id, json_dumps({'reason': reason, 'before': before, 'after': after}), '受控文件')
        return {'record_id': target_id, 'file_id': file_id}
