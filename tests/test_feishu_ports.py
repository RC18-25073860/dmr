import importlib.util
import sys
from types import SimpleNamespace
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
p=Path(__file__).resolve().parents[1]/'backend/feishu_integration.py'
spec=importlib.util.spec_from_file_location('ports',p);mod=importlib.util.module_from_spec(spec);sys.modules['ports']=mod;spec.loader.exec_module(mod)
def test_status_and_disabled_event():
 app=FastAPI();mod.install_feishu_integration(app,lambda r,a: SimpleNamespace(global_scope=True))
 with TestClient(app) as c:
  s=c.get('/api/integrations/feishu/status');assert s.status_code==200
  assert s.json()['status']=='not_connected'
  assert s.json()['cloud_writeback_enabled'] is False
  assert c.post('/api/integrations/feishu/events',json={'challenge':'test'}).status_code==503

def test_status_scope():
 app=FastAPI();mod.install_feishu_integration(app,lambda r,a: SimpleNamespace(global_scope=False))
 with TestClient(app) as c: assert c.get('/api/integrations/feishu/status').status_code==403
