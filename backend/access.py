"""DMR authorization policy, independent of the identity provider."""
from __future__ import annotations
from copy import deepcopy
from fastapi import HTTPException

DIMENSIONS = ('registrant', 'project_category', 'product')
UNKNOWN = {'', '待确认', '未知', None}

class AccessPolicy:
    def __init__(self, store, user):
        self.store, self.user = store, user

    @property
    def admin(self):
        return self.user['role'] == 'admin'

    @property
    def global_scope(self):
        return self.admin or any(all(s.get(k) == '*' for k in DIMENSIONS) for s in self.user['scopes'])

    @property
    def permissions(self):
        role = self.user['role']
        result = {'read', 'preview'}
        if self.admin or self.user['can_download']: result.add('download')
        if self.admin or self.user['can_history']: result.add('history')
        if role in {'admin', 'document_control', 'uploader'}: result.update({'analyze', 'review'})
        if role in {'admin', 'document_control'}: result.update({'commit', 'classify', 'resolve', 'correct_archive'})
        if self.global_scope and role in {'admin', 'document_control'}: result.update({'sync', 'notify', 'audit'})
        if self.admin: result.add('manage_users')
        return sorted(result)

    def require(self, permission):
        if permission not in self.permissions:
            raise HTTPException(403, '当前人员没有此操作权限')

    def matches(self, resource, incomplete=False):
        if self.admin: return True
        return any(all(scope.get(key) == '*' or scope.get(key) == resource.get(key)
                       or (incomplete and resource.get(key) in UNKNOWN)
                       for key in DIMENSIONS) for scope in self.user['scopes'])

    def require_resource(self, resource, incomplete=False):
        if not self.matches(resource, incomplete):
            raise HTTPException(403, '该资料超出当前人员获授权的范围')

    def record(self, record_id):
        row = self.store.execute('SELECT * FROM records WHERE id=?', (record_id,)).fetchone()
        if not row or not self.matches(dict(row)):
            raise HTTPException(404, '记录不存在或无权访问')
        return dict(row)

    def file(self, file_id):
        row = self.store.execute('SELECT * FROM files WHERE id=?', (file_id,)).fetchone()
        if not row: raise HTTPException(404, '文件不存在或无权访问')
        self.record(row['record_id'])
        if row['status'] != '当前有效' and 'history' not in self.permissions:
            raise HTTPException(403, '未获历史版本查阅权限')
        return row

    def owns_batch(self, batch_id):
        row = self.store.execute('SELECT owner_user_id FROM batches WHERE id=?', (batch_id,)).fetchone()
        return bool(row and row['owner_user_id'] == self.user['id'])

    def item_allowed(self, item, batch_id=None):
        own = self.owns_batch(batch_id or item.get('batch_id', ''))
        # Ownership only permits completion of missing fields, never an explicit foreign value.
        if not self.matches(item, incomplete=own): return False
        for key in ('record_id', 'context_record_id'):
            if item.get(key):
                try: self.record(item[key])
                except HTTPException: return False
        return True

    def batch(self, batch_id, commit=False):
        self.require('analyze' if not commit else 'commit')
        batch = self.store.get_batch(batch_id)
        rows = [dict(r) for r in self.store.execute('SELECT * FROM batch_items WHERE batch_id=?', (batch_id,)).fetchall()]
        if not rows or not all(self.item_allowed(row, batch_id) for row in rows):
            raise HTTPException(404, '批次不存在或超出授权范围')
        if commit:
            for row in rows:
                if row['status'] in {'matched', 'duplicate'}: self.require_resource(row)
        return batch

    def validate_upload(self, parsed):
        self.require_resource(parsed, incomplete=True)
        # Matching may consult an existing record. Never disclose its version outside scope.
        for row in self.store.find_records_for_item(parsed):
            self.require_resource(dict(row))

    def filtered_state(self):
        state = deepcopy(self.store.state())
        records = [r for r in state['records'] if self.matches(r)]
        ids = {r['id'] for r in records}
        if 'history' not in self.permissions:
            for r in records: r['previous'] = ''
        if not self.admin:
            # Parsing evidence can contain unrelated historical rows. Serve minimal display metadata.
            for r in records:
                r['metadata'] = {k:v for k,v in r.get('metadata', {}).items() if k in {'version_rule','version_date','document_version'}}
        files = [f for f in state['libraryFiles'] if f['recordId'] in ids and ('history' in self.permissions or f['status']=='当前有效')]
        if not self.admin:
            for f in files:
                if 'metadata' in f: f['metadata'] = {k:v for k,v in f['metadata'].items() if k in {'version_rule','version_date','document_version'}}
        state['records'], state['libraryFiles'] = records, files
        allowed_batches = set()
        if 'analyze' in self.permissions:
            candidate_batches = {b['id'] for b in state['batches']}
            candidate_batches.update(e['batchId'] for e in state['exceptions'] if e.get('batchId'))
            for batch_id in candidate_batches:
                try: self.batch(batch_id); allowed_batches.add(batch_id)
                except HTTPException: pass
        state['batches'] = [b for b in state['batches'] if b['id'] in allowed_batches]
        state['exceptions'] = [e for e in state['exceptions'] if 'review' in self.permissions and
                               (e.get('batchId') in allowed_batches or (e.get('recordId') in ids and not e.get('batchId')))]
        if 'audit' not in self.permissions:
            state.update(operationLogs=[], notifications=[], latestSyncChanges=[], latestSyncTime='', latestSyncBatch='',
                         latestSyncTrigger='需授权后执行', latestConfirmed=True, latestConfirmedBy='', todayUpdateCount=0)
        state['sourceHealth'] = [{'name':'授权范围内资料','detail':f'{len(records)} 条清单 · {len(files)} 份附件','time':'本地'}]
        state['fileSequence'] = len(files)+1
        state['currentUser'] = self.user
        state['permissions'] = self.permissions
        return state
