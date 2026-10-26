"""Local reminders for human cloud edits. This module never writes to Feishu."""
import json
import re
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, HTTPException, Request
from backend.feishu_catalog import load_snapshot
from backend.service import compare_versions, now_iso
from backend.catalog_locator import _locate_in_rows, _normalize_code
from backend.document_management import catalog_update_policy, json_dict


def _cell_text(cell):
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, dict):
        return _cell_text(cell.get('value', cell.get('text', '')))
    if isinstance(cell, list):
        return ''.join(_cell_text(c) for c in cell)
    return str(cell).strip() if cell is not None else ''


def _product_matches(product, text):
    # Alphanumeric boundaries prevent UI04 matching UI04S; ambiguous naming stays manual.
    codes = re.findall(r'[A-Za-z]+\d+[A-Za-z0-9]*', product.upper())
    return bool(codes) and all(re.search(r'(?<![A-Z0-9])' + re.escape(c) + r'(?![A-Z0-9])', text.upper()) for c in codes)


def _subject_key(subject):
    subject = (subject or '').strip()
    for suffix in ('科技有限公司', '有限公司', '公司'):
        if subject.endswith(suffix):
            subject = subject[:-len(suffix)]
    return subject


def _subject_matches(subject, text):
    subject = (subject or '').strip()
    if subject in ('', '待确认'):
        return False
    if subject in text:
        return True
    key = _subject_key(subject)
    return bool(key and key != subject and key in text)


def document_locations(document):
    """Resolve physical coordinates from snapshot indices, never array offsets."""
    locations = {}
    for sheet in document.get('sheets', []):
        rows = {}
        for block in sheet.get('ranges', []):
            for number, cells in zip(block.get('row_indices', []), block.get('cells', [])):
                rows.setdefault(number, {}).update({col: _cell_text(cell) for col, cell in zip(block.get('col_indices', []), cells)})
        headers = None
        for number, cells in sorted(rows.items()):
            code_cols = [col for col, value in cells.items() if value in ('编号', '文件编号', '物料编码')]
            version_cols = [col for col, value in cells.items() if value in ('版本', '版本号', '文件版本')]
            if code_cols or version_cols:
                headers = (code_cols[0], version_cols[0]) if len(code_cols) == len(version_cols) == 1 else None
                continue
            if not headers:
                continue
            code_col, version_col = headers
            code = cells.get(code_col, '')
            if not code or code in ('/', '／', '-', '—'):
                continue
            locations.setdefault(code, []).append({
                'sheet_name': sheet.get('sheet_name', ''), 'sheet_id': sheet.get('sheet_id', ''),
                'row': number, 'code_column': code_col, 'code_cell': f'{code_col}{number}',
                'version_column': version_col, 'version_cell': f'{version_col}{number}',
                'snapshot_version': cells.get(version_col, ''), 'read_at': document.get('read_at', '')})
    return {code: values[0] for code, values in locations.items() if len(values) == 1}


def document_has_candidate(document, record, filename):
    """Use cached content only to suggest lists; live reading still resolves positions."""
    if 'business_code' not in record.keys() or 'module' not in record.keys():
        return False
    for sheet in document.get('sheets', []):
        rows = {}
        for block in sheet.get('ranges', []):
            for number, cells in zip(block.get('row_indices', []), block.get('cells', [])):
                rows.setdefault(number, {}).update({col: _cell_text(cell) for col, cell in zip(block.get('col_indices', []), cells)})
        matches, _ = _locate_in_rows(rows, _normalize_code(record['business_code']),
                                     sheet.get('sheet_id', ''), sheet.get('sheet_name', ''),
                                     document.get('read_at', ''), filename=filename, module=record['module'])
        if matches:
            return True
    return False


def _norm_component(value):
    return str(value).strip().upper() if value is not None else ''


def _component_lookup_values(item):
    metadata = {}
    for key in ('record_metadata', 'file_metadata'):
        value = item.get(key, {})
        if isinstance(value, str):
            value = json_dict(value)
        if isinstance(value, dict):
            metadata.update(value)
    identifiers = metadata.get('identifiers') if isinstance(metadata.get('identifiers'), dict) else {}
    material_values = {
        _norm_component(value)
        for value in (item.get('code'), identifiers.get('material_no'), metadata.get('material_no'))
        if _norm_component(value)
    }
    drawing_values = {
        _norm_component(value)
        for value in (identifiers.get('drawing_no'), metadata.get('drawing_no'))
        if _norm_component(value)
    }
    if item.get('management_target') == 'structural_drawing_register' and _norm_component(item.get('code')):
        drawing_values.add(_norm_component(item.get('code')))
    return {'material': material_values, 'drawing': drawing_values}


class ManualCatalogUpdates:
    def __init__(self, store, config):
        self.store = store
        self.path = Path(config.data_dir) / 'feishu_catalog.json'

    def documents(self):
        return load_snapshot(self.path)['documents']

    def _documents_by_token(self):
        return {doc['token']: doc for doc in self.documents()}

    def _target_from_doc(self, doc, *, status='pending', note='', association_confirmed=False):
        return {'document_token': doc['token'], 'document_name': doc['name'], 'document_url': doc['url'],
                'status': status, 'processed_by': '', 'processed_user_id': '', 'processed_at': '',
                'note': note, 'association_confirmed': association_confirmed}

    def _normalize_targets(self, item):
        targets = []
        for raw in item.get('targets') or []:
            if not isinstance(raw, dict) or not raw.get('document_token'):
                continue
            targets.append({
                'document_token': raw.get('document_token', ''),
                'document_name': raw.get('document_name', ''),
                'document_url': raw.get('document_url', ''),
                'status': raw.get('status') if raw.get('status') in {'pending', 'processed'} else 'pending',
                'processed_by': raw.get('processed_by', ''),
                'processed_user_id': raw.get('processed_user_id', ''),
                'processed_at': raw.get('processed_at', ''),
                'note': raw.get('note', ''),
                'association_confirmed': bool(raw.get('association_confirmed')),
            })
        if not targets and item.get('document_token'):
            targets.append({'document_token': item.get('document_token', ''),
                            'document_name': item.get('document_name', ''),
                            'document_url': item.get('document_url', ''),
                            'status': 'processed' if item.get('status') == 'processed' else 'pending',
                            'processed_by': item.get('processed_by', ''),
                            'processed_user_id': item.get('processed_user_id', ''),
                            'processed_at': item.get('processed_at', ''),
                            'note': item.get('note', ''),
                            'association_confirmed': bool(item.get('manual_confirmation'))})
        item['targets'] = targets
        item['target_count'] = len(targets)
        item['processed_count'] = sum(1 for target in targets if target['status'] == 'processed')
        if targets:
            first = next((target for target in targets if target['status'] == 'pending'), targets[0])
            item['document_token'] = first['document_token']
            item['document_name'] = first['document_name']
            item['document_url'] = first['document_url']
        return item

    def _apply_management_policy(self, item, record=None, file=None):
        if record is None:
            record = {
                'business_code': item.get('code', ''),
                'registrant': item.get('registrant', ''),
                'project_category': item.get('project_category', ''),
                'product': item.get('product', ''),
                'module': item.get('module', ''),
                'linked_bom': item.get('linked_bom', ''),
                'metadata': item.get('record_metadata', {}),
            }
        if file is None:
            file = {
                'name': item.get('file_name', ''),
                'linked_bom': item.get('linked_bom', ''),
                'metadata': item.get('file_metadata', {}),
            }
        item.update(catalog_update_policy(record, file))
        item['direct_dmr'] = bool(item.get('direct_catalog_row'))
        item['related_boms'] = [
            candidate for candidate in item.get('relation_candidates', [])
            if isinstance(candidate, dict) and candidate.get('type') == 'bom'
        ]
        return item

    def _bom_references(self, access, item):
        if item.get('management_target') not in {'packaging_drawing_register', 'structural_drawing_register', 'ack_spec_register'}:
            return []
        values = _component_lookup_values(item)
        if not values['material'] and not values['drawing']:
            return []
        rows = self.store.execute(
            """SELECT records.id AS record_id, records.business_code, records.registrant, records.project_category,
                      records.product, records.module, records.metadata AS record_metadata,
                      files.id AS file_id, files.name AS file_name, files.metadata AS file_metadata
               FROM records
               JOIN files ON files.record_id=records.id
               WHERE records.module='BOM' AND files.status='当前有效'"""
        ).fetchall()
        candidates = []
        seen = set()
        for row in rows:
            resource = {'registrant': row['registrant'], 'project_category': row['project_category'], 'product': row['product']}
            if not access.matches(resource):
                continue
            metadata = {**json_dict(row['record_metadata']), **json_dict(row['file_metadata'])}
            for component in metadata.get('bom_components') if isinstance(metadata.get('bom_components'), list) else []:
                if not isinstance(component, dict):
                    continue
                hits = []
                material = _norm_component(component.get('material_no'))
                drawing = _norm_component(component.get('drawing_no'))
                if material and material in values['material']:
                    hits.append(('material_no', component.get('material_no')))
                if drawing and drawing in values['drawing']:
                    hits.append(('drawing_no', component.get('drawing_no')))
                for field, value in hits:
                    key = (row['business_code'], str(component.get('row', '')), field, _norm_component(value))
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append({
                        'type': 'bom',
                        'basis': 'bom_component',
                        'confidence': 'candidate',
                        'code': row['business_code'],
                        'product': row['product'],
                        'registrant': row['registrant'],
                        'row': str(component.get('row', '')),
                        'matched_field': field,
                        'matched_value': str(value).strip() if value is not None else '',
                        'record_id': row['record_id'],
                        'file_id': row['file_id'],
                        'file_name': row['file_name'],
                    })
        return candidates

    def _append_visible_bom_references(self, access, item):
        references = self._bom_references(access, item)
        if not references:
            return item
        existing = list(item.get('relation_candidates') or [])
        seen = {
            (c.get('basis'), c.get('code'), c.get('row'), c.get('matched_field'), c.get('matched_value'))
            for c in existing if isinstance(c, dict)
        }
        for candidate in references:
            key = (candidate.get('basis'), candidate.get('code'), candidate.get('row'), candidate.get('matched_field'), candidate.get('matched_value'))
            if key not in seen:
                existing.append(candidate)
                seen.add(key)
        item['relation_candidates'] = existing
        item['related_boms'] = [
            candidate for candidate in existing
            if isinstance(candidate, dict) and candidate.get('type') == 'bom'
        ]
        return item

    def _validate_current_identity(self, access, row, item):
        record = access.record(row['record_id'])
        file = self.store.execute('SELECT * FROM files WHERE id=?', (row['file_id'],)).fetchone()
        changed = (not file or file['status'] != '当前有效' or file['record_id'] != record['id'] or
                   any(record[k] != item[j] for k, j in [('business_code','code'),('registrant','registrant'),('product','product'),('module','module'),('version','new_version')]))
        if changed:
            raise HTTPException(409, '当前文件或归属已变化，请重新核对归档，不能确认旧提醒')

    def _confirmed_targets_for_record(self, record):
        try:
            docs_by_token = self._documents_by_token()
        except HTTPException:
            return []
        rows = self.store.execute(
            "SELECT payload FROM manual_catalog_updates WHERE record_id=? ORDER BY revision DESC, updated_at DESC",
            (record['id'],)).fetchall()
        for row in rows:
            item = self._normalize_targets(json.loads(row['payload']))
            if any(record[k] != item.get(j, '') for k, j in [('business_code','code'),('registrant','registrant'),('product','product'),('module','module')]):
                continue
            if not item.get('manual_confirmation'):
                continue
            targets = []
            seen = set()
            for target in item.get('targets', []):
                token = target['document_token']
                if token in seen or token not in docs_by_token:
                    continue
                if not target.get('association_confirmed'):
                    continue
                doc = docs_by_token[token]
                inherited = self._target_from_doc(doc, association_confirmed=True)
                targets.append(inherited)
                seen.add(token)
            return targets
        return []

    def match(self, record, filename="", *, documents=None):
        try:
            documents = self.documents() if documents is None else documents
        except HTTPException:
            documents = []
        candidates = []
        shared = []
        subject = (record['registrant'] or '').strip()
        subject_known = subject not in ('', '待确认')
        category = record['project_category'] if 'project_category' in record.keys() else ''
        for doc in documents:
            context = ' / '.join(doc['path']) + ' / ' + doc['name']
            categories = [value for value in ('医疗', '家电') if value in context]
            category_conflict = category in ('医疗', '家电') and categories and category not in categories
            if _product_matches(record['product'], context) and not category_conflict:
                if not subject_known or _subject_matches(subject, context):
                    candidates.append(doc)
                else:
                    shared.append(doc)
            elif document_has_candidate(doc, record, filename):
                shared.append(doc)
        # Document association does not depend on a matching row. Live location is separate.
        chosen = candidates[0] if subject_known and len(candidates) == 1 else None
        result = {'document_token': chosen['token'] if chosen else '',
                'document_name': chosen['name'] if chosen else '',
                'document_url': chosen['url'] if chosen else '',
                'candidate_tokens': [d['token'] for d in candidates + shared],
                'shared_candidate_tokens': [d['token'] for d in shared],
                'match_status': 'matched' if chosen else ('ambiguous' if candidates or shared else 'unmatched')}
        if chosen:
            result['targets'] = [self._target_from_doc(chosen)]
        return result

    def _active_notice_matches(self, row, record, file):
        try:
            item = json.loads(row['payload'])
        except (TypeError, json.JSONDecodeError):
            return False
        return (item.get('record_id') == record['id'] and item.get('file_id') == file['id'] and
                item.get('code') == record['business_code'] and item.get('registrant') == record['registrant'] and
                item.get('product') == record['product'] and item.get('module') == record['module'] and
                item.get('new_version') == file['version'])

    def _enqueue_file(self, file_id, *, batch_id='', actor=None, committed_ids=None, supersede_file_pending=False):
        file = self.store.execute('SELECT * FROM files WHERE id=?', (file_id,)).fetchone()
        if not file or file['status'] != '当前有效':
            return ''
        record = self.store.execute('SELECT * FROM records WHERE id=?', (file['record_id'],)).fetchone()
        if not record or record['version'] != file['version']:
            return ''
        now = now_iso()
        active = self.store.execute("SELECT * FROM manual_catalog_updates WHERE file_id=? AND status IN ('pending','processed')", (file['id'],)).fetchall()
        if any(self._active_notice_matches(row, record, file) for row in active):
            return ''
        replacement = active[0] if supersede_file_pending and active else None
        old = record['previous']
        # A single batch can archive V2 and V3 together. V2 is not the cloud's
        # starting version: exclude this batch's newly archived history.
        committed_ids = committed_ids or set()
        siblings = self.store.execute('SELECT id,version FROM files WHERE record_id=?', (record['id'],)).fetchall()
        if any(s['id'] in committed_ids and s['version'] == old for s in siblings):
            prior_versions = [s['version'] for s in siblings if s['id'] not in committed_ids]
            old = ''
            for version in prior_versions:
                comparison = compare_versions(version, file['version'])
                if comparison is not None and comparison < 0:
                    newest = compare_versions(version, old) if old else 1
                    if newest is not None and newest > 0:
                        old = version
        comparison = compare_versions(file['version'], old) if old else None
        if old and (comparison is None or comparison <= 0):
            return ''
        exclude_replacement = (replacement['id'],) if replacement else ('',)
        prior = self.store.execute(
            "SELECT * FROM manual_catalog_updates WHERE record_id=? AND status='pending' AND id<>? ORDER BY revision DESC",
            (record['id'], exclude_replacement[0])).fetchone()
        latest = self.store.execute('SELECT MAX(revision) AS value FROM manual_catalog_updates WHERE record_id=? AND id<>?', (record['id'], exclude_replacement[0])).fetchone()
        revision = (latest['value'] or 0) + 1
        match = self.match(record, file['name'])
        inherited = self._confirmed_targets_for_record(record)
        if inherited:
            match.update({'targets': inherited, 'document_token': inherited[0]['document_token'],
                          'document_name': inherited[0]['document_name'], 'document_url': inherited[0]['document_url'],
                          'match_status': 'matched', 'association_source': 'confirmed_history',
                          'candidate_tokens': [target['document_token'] for target in inherited]})
        item = {'id': replacement['id'] if replacement else 'CATUPDATE-' + uuid.uuid4().hex, 'record_id': record['id'],
                'file_id': file['id'], 'file_name': file['name'], 'revision': revision,
                'code': record['business_code'], 'registrant': record['registrant'],
                'product': record['product'], 'module': record['module'],
                'project_category': record['project_category'],
                'linked_bom': file['linked_bom'] or record['linked_bom'],
                'record_metadata': json.loads(record['metadata'] or '{}'),
                'file_metadata': json.loads(file['metadata'] or '{}'),
                'old_version': json.loads(prior['payload'])['old_version'] if prior else old,
                'new_version': file['version'], 'status': 'pending',
                'reason': 'version_update' if old else 'first_archive',
                'created_at': (replacement['created_at'] if replacement else (prior['created_at'] if prior else now)), 'updated_at': now,
                'processed_by': '', 'processed_at': '', 'note': '',
                'created_by': actor.get('display_name', '') if isinstance(actor, dict) else str(actor),
                'download_url': '/api/download/' + quote(file['id'], safe=''), **match}
        self._apply_management_policy(item, record, file)
        self._normalize_targets(item)
        self.store.execute("UPDATE manual_catalog_updates SET status='superseded',updated_at=? WHERE record_id=? AND status='pending' AND id<>?", (now, record['id'], item['id']))
        if replacement:
            self.store.execute("UPDATE manual_catalog_updates SET record_id=?,revision=?,status='pending',payload=?,created_at=?,updated_at=? WHERE id=?",
                               (item['record_id'], revision, json.dumps(item, ensure_ascii=False), item['created_at'], now, item['id']))
        else:
            self.store.execute('INSERT INTO manual_catalog_updates(id,record_id,file_id,revision,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                               (item['id'], item['record_id'], item['file_id'], revision, 'pending', json.dumps(item, ensure_ascii=False), item['created_at'], now))
        self.store.add_log('清单待人工更新', item['code'], f"{item['old_version']} → {item['new_version']}；仅生成提醒，未写入飞书。", batch_id, '待处理')
        return item['id']

    def enqueue_commit(self, result, actor):
        """Called inside the same transaction as commit_batch; never backfills old records."""
        created = []
        committed_ids = {r.get('file_id') for r in result.get('results', []) if r.get('status') == 'committed'}
        for outcome in result.get('results', []):
            if outcome.get('status') != 'committed' or not outcome.get('file_id'):
                continue
            notice_id = self._enqueue_file(outcome['file_id'], batch_id=result.get('batch_id', ''),
                                           actor=actor, committed_ids=committed_ids)
            if notice_id:
                created.append(notice_id)
        return created

    def enqueue_archive_correction(self, result, actor):
        """Create a fresh local reminder after a controlled-file reassignment."""
        with self.store.lock, self.store.conn.immediate():
            notice_id = self._enqueue_file(result.get('file_id'), batch_id='归档更正', actor=actor, supersede_file_pending=True)
            return [notice_id] if notice_id else []

    def listing(self, access):
        error = ''
        try:
            documents = self.documents()
            docs = [{**{k: d[k] for k in ('token', 'name', 'url', 'path')}, 'locations': document_locations(d)} for d in documents]
        except HTTPException as exc:
            docs, error = [], str(exc.detail)
        rows = self.store.execute("SELECT payload FROM manual_catalog_updates WHERE status IN ('pending','processed') ORDER BY updated_at DESC").fetchall()
        items = [self._normalize_targets(json.loads(r['payload'])) for r in rows]
        if not error:
            for item in items:
                self._apply_management_policy(item)
                self._append_visible_bom_references(access, item)
                recommendations = self.match({'registrant': item.get('registrant', ''), 'product': item.get('product', ''),
                                              'project_category': item.get('project_category', ''),
                                              'business_code': item.get('code', ''), 'module': item.get('module', '')},
                                             item.get('file_name', ''), documents=documents)
                item['candidate_tokens'] = recommendations['candidate_tokens']
                item['shared_candidate_tokens'] = recommendations['shared_candidate_tokens']
        return {'items': items, 'can_manage': all(p in access.permissions for p in ('commit', 'audit')),
                'catalog_documents': docs, 'catalog_error': error}

    def update_associations(self, access, notice_id, payload):
        access.require('commit')
        access.require('audit')
        with self.store.lock, self.store.conn.immediate():
            row = self.store.execute('SELECT * FROM manual_catalog_updates WHERE id=?', (notice_id,)).fetchone()
            if not row:
                raise HTTPException(404, '提醒不存在')
            if row['status'] not in {'pending', 'processed'} or type(payload.get('revision')) is not int or payload['revision'] != row['revision']:
                raise HTTPException(409, '提醒已更新，请刷新后核对')
            note = payload.get('note', '')
            if not isinstance(note, str) or not note.strip() or len(note) > 2000:
                raise HTTPException(400, '请填写本次关联调整说明，最多2000字')
            tokens = payload.get('document_tokens')
            if not isinstance(tokens, list) or not tokens or any(not isinstance(token, str) or not token.strip() for token in tokens):
                raise HTTPException(400, '请选择至少一份对应清单')
            tokens = [token.strip() for token in tokens]
            if len(tokens) != len(set(tokens)):
                raise HTTPException(400, '对应清单不能重复')
            docs_by_token = self._documents_by_token()
            missing = [token for token in tokens if token not in docs_by_token]
            if missing:
                raise HTTPException(400, '请选择当前目录中的对应飞书清单')
            item = self._normalize_targets(json.loads(row['payload']))
            self._apply_management_policy(item)
            self._append_visible_bom_references(access, item)
            self._validate_current_identity(access, row, item)
            existing = {target['document_token']: target for target in item.get('targets', [])}
            processed_tokens = {token for token, target in existing.items() if target['status'] == 'processed'}
            if not processed_tokens.issubset(set(tokens)):
                raise HTTPException(400, '已处理的清单不能从关联中移除')
            targets = []
            for token in tokens:
                if token in existing:
                    target = dict(existing[token])
                    target['association_confirmed'] = True
                    if target['status'] != 'processed':
                        target['note'] = note.strip()
                else:
                    target = self._target_from_doc(docs_by_token[token], note=note.strip(), association_confirmed=True)
                target['document_name'] = docs_by_token[token]['name']
                target['document_url'] = docs_by_token[token]['url']
                targets.append(target)
            now = now_iso()
            new_revision = row['revision'] + 1
            item.update(targets=targets, revision=new_revision, updated_at=now,
                        association_note=note.strip(), manual_confirmation=True)
            self._normalize_targets(item)
            done = item['target_count'] > 0 and item['processed_count'] == item['target_count']
            item['status'] = 'processed' if done else 'pending'
            self.store.execute("UPDATE manual_catalog_updates SET status=?,revision=?,payload=?,updated_at=? WHERE id=?",
                               (item['status'], new_revision, json.dumps(item, ensure_ascii=False), now, notice_id))
            self.store.add_log('清单关联调整', item['code'], f"确认{len(targets)}份对应清单；说明：{note.strip()}", notice_id,
                               '成功' if done else '待处理', user=f"{access.user['display_name']} [{access.user['id']}]")
            return item

    def resolve(self, access, notice_id, payload):
        access.require('commit')
        access.require('audit')
        with self.store.lock, self.store.conn.immediate():
            row = self.store.execute('SELECT * FROM manual_catalog_updates WHERE id=?', (notice_id,)).fetchone()
            if not row:
                raise HTTPException(404, '提醒不存在')
            if row['status'] != 'pending' or type(payload.get('revision')) is not int or payload['revision'] != row['revision']:
                raise HTTPException(409, '提醒已更新或已处理，请刷新后核对')
            item = self._normalize_targets(json.loads(row['payload']))
            self._apply_management_policy(item)
            self._append_visible_bom_references(access, item)
            self._validate_current_identity(access, row, item)
            note = payload.get('note', '')
            if not isinstance(note, str) or len(note) > 2000:
                raise HTTPException(400, '处理说明最多2000字')
            if payload.get('document_token'):
                token = payload.get('document_token')
            elif not item.get('direct_catalog_row', True) and note.strip():
                token = ''
            elif len(item.get('targets', [])) == 1:
                token = item['targets'][0]['document_token']
            else:
                raise HTTPException(400, '多份清单待处理时，请明确选择要确认的清单')
            doc = next((d for d in self.documents() if d['token'] == token), None) if token else None
            if token and not doc:
                raise HTTPException(400, '请选择当前目录中的对应飞书清单')
            targets = item.get('targets', [])
            target = next((target for target in targets if target['document_token'] == token), None) if token else None
            if not token:
                external_token = 'external:' + (item.get('management_target') or 'manual_register')
                target = {
                    'document_token': external_token,
                    'document_name': item.get('management_target_label', '外部台账'),
                    'document_url': '',
                    'status': 'pending',
                    'processed_by': '',
                    'processed_user_id': '',
                    'processed_at': '',
                    'note': '',
                    'association_confirmed': True,
                }
                targets = [target]
                item['targets'] = targets
            elif not target:
                target = self._target_from_doc(doc)
                targets.append(target)
                item['targets'] = targets
            if target['status'] == 'processed':
                raise HTTPException(409, '这份清单已经确认处理，请刷新后核对')
            auto_matched = item.get('match_status') == 'matched' and token == item.get('document_token') and not item.get('association_source')
            if not (auto_matched or target.get('association_confirmed') or not item.get('direct_catalog_row', True)) and not note.strip():
                raise HTTPException(400, '人工指定清单时请填写对应关系和处理说明')
            now = now_iso()
            target.update(document_token=doc['token'] if doc else target['document_token'],
                          document_name=doc['name'] if doc else target['document_name'],
                          document_url=doc['url'] if doc else '', status='processed',
                          processed_by=access.user['display_name'], processed_user_id=access.user['id'], processed_at=now,
                          note=note.strip(), association_confirmed=True)
            self._normalize_targets(item)
            done = item['target_count'] > 0 and item['processed_count'] == item['target_count']
            new_revision = row['revision'] + 1
            item.update(status='processed' if done else 'pending', revision=new_revision,
                        document_token=doc['token'] if doc else target['document_token'],
                        document_name=doc['name'] if doc else target['document_name'],
                        document_url=doc['url'] if doc else item.get('document_url', ''),
                        processed_by=access.user['display_name'] if done else '',
                        processed_user_id=access.user['id'] if done else '',
                        processed_at=now if done else '', updated_at=now, note=note.strip(), manual_confirmation=True)
            self.store.execute("UPDATE manual_catalog_updates SET status=?,revision=?,payload=?,updated_at=? WHERE id=?",
                               (item['status'], new_revision, json.dumps(item, ensure_ascii=False), now, notice_id))
            target_name = doc['name'] if doc else item.get('management_target_label', '外部台账')
            self.store.add_log('清单人工确认', item['code'], f"{item['old_version']} → {item['new_version']}；文控确认已处理，系统未验证飞书内容；清单：{target_name}；说明：{note.strip()}", notice_id,
                               '成功' if done else '待处理', user=f"{access.user['display_name']} [{access.user['id']}]")
            return item


def install_manual_catalog_updates(app, store, config, policy):
    service = ManualCatalogUpdates(store, config)
    def authorize(request):
        access = policy(request, 'read')
        if not access.global_scope:
            raise HTTPException(403, '清单更新提醒只开放给全局授权人员')
        return access

    def require_fresh_directory(*, force=False):
        refresh = getattr(app.state, 'catalog_refresh', None)
        if refresh is None:
            raise HTTPException(503, '实时目录服务不可用，请稍后重试')
        state = refresh.ensure_fresh(force=force, wait=True, wait_timeout=120)
        if state.get('status') != 'success' or state.get('running'):
            raise HTTPException(503, '目录仍在刷新，请稍后重试' if state.get('running') else '未能读取最新飞书目录，请检查授权和网络后刷新；旧目录不用于新的定位')
        return state

    @app.post('/api/catalog-updates/refresh-list')
    def refresh_updates(request: Request):
        access = authorize(request)
        require_fresh_directory(force=True)
        with store.lock:
            return service.listing(access)

    @app.get('/api/catalog-updates')
    def get_updates(request: Request):
        access = authorize(request)
        with store.lock:
            return service.listing(access)

    @app.post('/api/catalog-locate')
    def locate_catalog(request: Request, payload: dict = Body(...)):
        from backend.catalog_locator import read_live_location
        access = authorize(request)
        access.require('commit')
        access.require('audit')
        token, code = payload.get('document_token'), payload.get('code', '')
        filename, module = payload.get('filename', ''), payload.get('module', '')
        if not isinstance(filename, str) or len(filename) > 500 or not isinstance(module, str) or len(module) > 80:
            raise HTTPException(400, '\u6587\u4ef6\u540d\u79f0\u6216\u7c7b\u522b\u683c\u5f0f\u65e0\u6548')
        proposed_version = payload.get('proposed_version', '')
        if not isinstance(proposed_version, str) or len(proposed_version) > 120:
            raise HTTPException(400, '版本格式无效')
        if not isinstance(code, str) or len(code) > 500 or (not code.strip() and not filename.strip() and not payload.get('notice_id')):
            raise HTTPException(400, '请输入有效文件编号')
        require_fresh_directory()
        with store.lock:
            document = next((d for d in service.documents() if d['token'] == token), None)
            if not document:
                raise HTTPException(400, '请选择已关联的清单')
            notice_id = payload.get('notice_id')
            if notice_id:
                row = store.execute('SELECT * FROM manual_catalog_updates WHERE id=?', (notice_id,)).fetchone()
                if not row or row['status'] != 'pending' or row['revision'] != payload.get('revision'):
                    raise HTTPException(409, '提醒已变化，请刷新后重新定位')
                item = json.loads(row['payload'])
                service._apply_management_policy(item)
                proposed_version = item['new_version']
                filename, module = item['file_name'], item['module']
                record = access.record(row['record_id'])
                if code.strip() != item['code'] or record['business_code'] != code.strip() or record['version'] != item['new_version']:
                    raise HTTPException(409, '文件或归属已变化，请重新核对')
                if not item.get('catalog_locator_enabled', True):
                    return {'status': 'not_applicable', 'entry_status': 'external_register',
                            'document_token': document['token'], 'document_name': document['name'], 'document_url': document['url'],
                            'code': code.strip(), 'matches': [], 'cloud_writes': 0,
                            'management_target': item.get('management_target', ''),
                            'management_target_label': item.get('management_target_label', ''),
                            'management_instruction': item.get('management_instruction', ''),
                            'message': item.get('catalog_location_hint') or '此类文件不在量产 DMR 清单中自动定位。'}
        # Network reads run outside the archive transaction and never change Feishu.
        result = read_live_location(document, code.strip(), config=config, filename=filename, module=module)
        if notice_id:
            with store.lock:
                latest = store.execute('SELECT status,revision FROM manual_catalog_updates WHERE id=?', (notice_id,)).fetchone()
                if not latest or latest['status'] != 'pending' or latest['revision'] != payload.get('revision'):
                    raise HTTPException(409, '定位期间提醒已变化，请刷新')
        current_version = result.get('location', {}).get('current_version', '')
        comparison = compare_versions(proposed_version, current_version) if proposed_version and current_version else None
        if 'matches' in result:
            result['matches'] = [{**match, 'version_comparison': compare_versions(proposed_version, match.get('current_version', '')) if proposed_version and match.get('current_version') else None} for match in result['matches']]
        return {**result, 'version_comparison': comparison, 'proposed_version': proposed_version, 'document_name': document['name'], 'document_url': document['url'], 'code': code.strip()}

    @app.post('/api/catalog-updates/{notice_id}/resolve')
    def resolve_update(notice_id: str, request: Request, payload: dict = Body(...)):
        return service.resolve(authorize(request), notice_id, payload)

    @app.post('/api/catalog-updates/{notice_id}/associations')
    def update_associations(notice_id: str, request: Request, payload: dict = Body(...)):
        return service.update_associations(authorize(request), notice_id, payload)

    return service
