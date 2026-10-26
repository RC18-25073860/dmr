"""Boundary contracts with explicit test doubles, not a Feishu signature implementation."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.feishu_integration import CatalogChange, install_feishu_integration


class TestVerifier:
    __test__ = False

    def verify_and_decode(self, body, headers):
        if headers.get('x-test-auth') != 'valid':
            raise ValueError('private verifier diagnostic')
        return CatalogChange(**json.loads(body))


class DurableTestQueue:
    """Test fixture modeling the required atomic queue contract, without a worker."""
    def __init__(self, path):
        self.path = path
        self.fail_before = False
        self.fail_after = False
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (event_id TEXT PRIMARY KEY, payload TEXT)')

    def enqueue(self, change):
        if self.fail_before:
            self.fail_before = False
            raise OSError('private queue diagnostic')
        payload = json.dumps(vars(change), sort_keys=True)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?)', (change.event_id, payload))
            existing = db.execute('SELECT payload FROM jobs WHERE event_id=?', (change.event_id,)).fetchone()[0]
            if existing != payload:
                raise ValueError('Conflicting event ID')
        if self.fail_after:
            self.fail_after = False
            raise OSError('uncertain commit')
        return change.event_id

    def count(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]


def client(verifier=None, queue=None):
    app = FastAPI()
    install_feishu_integration(app, lambda *_: SimpleNamespace(global_scope=True), verifier=verifier, queue=queue)
    return TestClient(app)


def post(c, **changes):
    return c.post('/api/integrations/feishu/events', headers={'x-test-auth': 'valid'},
                  json={'event_id': 'event-1', 'document_token': 'doc-1', 'change_type': 'changed', **changes})


@pytest.mark.parametrize('which', ['none', 'verifier', 'queue'])
def test_partial_configuration_stays_closed(tmp_path, which):
    queue = DurableTestQueue(tmp_path / 'queue.db')
    with client(TestVerifier() if which == 'verifier' else None, queue if which == 'queue' else None) as c:
        assert post(c).status_code == 503
        assert c.get('/api/integrations/feishu/status').json()['platform_catalog']['events_enabled'] is False
    assert queue.count() == 0


def test_verification_rejection_never_enqueues(tmp_path):
    queue = DurableTestQueue(tmp_path / 'queue.db')
    with client(TestVerifier(), queue) as c:
        response = c.post('/api/integrations/feishu/events', json={'challenge': 'do-not-echo'})
        assert response.status_code == 403
        assert 'private' not in response.text and 'do-not-echo' not in response.text
        assert post(c, change_type='unsupported').status_code == 403
        assert post(c, event_id='').status_code == 403
    assert queue.count() == 0


def test_duplicate_survives_app_and_queue_restart(tmp_path):
    path = tmp_path / 'queue.db'
    for _ in range(2):
        queue = DurableTestQueue(path)
        with client(TestVerifier(), queue) as c:
            for _ in range(2):
                response = post(c)
                assert response.status_code == 202
                assert response.json() == {'status': 'accepted', 'job_id': 'event-1'}
            status = c.get('/api/integrations/feishu/status').json()
            assert status['status'] == 'events_configured'
            assert status['cloud_writeback_enabled'] is False
            assert status['platform_catalog']['scheduled_sync_enabled'] is False
    assert queue.count() == 1


@pytest.mark.parametrize('failure', ['fail_before', 'fail_after'])
def test_uncertain_enqueue_is_retryable(tmp_path, failure):
    queue = DurableTestQueue(tmp_path / 'queue.db')
    setattr(queue, failure, True)
    with client(TestVerifier(), queue) as c:
        response = post(c)
        assert response.status_code == 503
        assert 'private' not in response.text
        assert post(c).status_code == 202
    assert queue.count() == 1


def test_conflicting_duplicate_does_not_overwrite(tmp_path):
    queue = DurableTestQueue(tmp_path / 'queue.db')
    with client(TestVerifier(), queue) as c:
        assert post(c).status_code == 202
        assert post(c, document_token='different-doc').status_code == 503
        assert post(c).status_code == 202
    assert queue.count() == 1


def test_empty_queue_ack_is_not_accepted():
    with client(TestVerifier(), SimpleNamespace(enqueue=lambda _: '')) as c:
        assert post(c).status_code == 503


@pytest.mark.parametrize('configured', [False, True])
def test_create_app_bootstrap_injection(tmp_path, configured):
    from pathlib import Path
    from backend.config import AppConfig
    from backend.service import DMRStore
    from server import create_app

    root = Path(__file__).resolve().parents[1]
    config = AppConfig(root=root, data_dir=tmp_path, database_url=None,
                       auth_mode='local', static_root=root / 'frontend' / 'dist')
    store = DMRStore(tmp_path, None)
    queue = DurableTestQueue(tmp_path / 'events.db')
    dependencies = ({'feishu_event_verifier': TestVerifier(), 'feishu_sync_queue': queue}
                    if configured else {})
    try:
        app = create_app(store, config, **dependencies)
        with TestClient(app, base_url='http://127.0.0.1') as c:
            response = post(c)
            assert response.status_code == (202 if configured else 503)
            assert queue.count() == (1 if configured else 0)
            if configured:
                assert c.post('/api/integrations/feishu/events', json={}).status_code == 403
                assert post(c).json() == response.json()
                assert queue.count() == 1
    finally:
        store.close()
