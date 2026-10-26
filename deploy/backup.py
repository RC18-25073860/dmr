#!/usr/bin/env python3
"""Offline backup and restore to a NEW directory only. Never edits source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.runtime_lock import acquire


def digest(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()


def check_db(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('数据库完整性校验失败')


def files(root):
    for p in root.rglob('*'):
        if p.is_symlink(): raise ValueError('数据目录不能包含软链接')
        if p.is_file() and p.name != '.runtime.lock': yield p


def transfer(source, target, restore=False, stopped=False):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not stopped: raise ValueError('必须先停止平台及所有写入任务，再传入 --stopped')
    if target.exists() or source == target or source in target.parents or target in source.parents:
        raise ValueError('目标必须是数据目录之外的全新目录，禁止覆盖')
    if not (source/'dmr.sqlite3').is_file(): raise ValueError('缺少dmr.sqlite3')
    lock = acquire(source)
    temp = None
    try:
        check_db(source/'dmr.sqlite3')
        source_files = list(files(source))
        if restore:
            manifest = json.loads((source/'backup-manifest.json').read_text())
            actual = {str(p.relative_to(source)): digest(p) for p in source_files if p.name != 'backup-manifest.json'}
            if manifest['sha256'] != actual: raise ValueError('备份文件集合或校验值不一致')
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix='.dmr-backup-', dir=target.parent))
        os.chmod(temp, 0o700)
        for p in source_files:
            if p.name == 'backup-manifest.json': continue
            dst = temp/p.relative_to(source); dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
        manifest = {'format':1,'sha256':{str(p.relative_to(temp)):digest(p) for p in files(temp)}}
        (temp/'backup-manifest.json').write_text(json.dumps(manifest, indent=2))
        check_db(temp/'dmr.sqlite3')
        if target.exists(): raise ValueError('目标已存在，拒绝覆盖')
        temp.rename(target); temp=None
    finally:
        if temp is not None: shutil.rmtree(temp)
        lock.close()
    return {'target': str(target), 'files':len(manifest['sha256']), 'verified':True}


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['backup','restore']);p.add_argument('source');p.add_argument('target');p.add_argument('--stopped',action='store_true')
    a=p.parse_args()
    try: print(json.dumps(transfer(a.source,a.target,a.mode=='restore',a.stopped),ensure_ascii=False))
    except Exception as e: print(str(e),file=sys.stderr);sys.exit(1)
