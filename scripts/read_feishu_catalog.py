#!/usr/bin/env python3
"""Read-only Feishu catalog snapshot. Uses only list/workbook-info/cells-get.

No cloud writes and no model calls. Publish atomically only after all reads pass.
Run with the user's authorized lark-cli account. Credentials never enter snapshot.
"""
from __future__ import annotations
import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import fcntl

ROOT_TOKEN = os.environ.get('DMR_FEISHU_ROOT_TOKEN', '').strip() or 'ExampleRootFolderToken000001'
HOST = os.environ.get('DMR_FEISHU_HOST', 'https://example.feishu.cn').rstrip('/')
READ_COMMANDS = {('drive','files','list'), ('sheets','+workbook-info'), ('sheets','+cells-get')}

def lark_cli_command():
    return os.environ.get('LARK_CLI') or 'lark-cli'

def call(*args):
    prefix = tuple(args[:3]) if args[0] == 'drive' else tuple(args[:2])
    if prefix not in READ_COMMANDS:
        raise ValueError('Only cloud read commands are allowed')
    result = subprocess.run([lark_cli_command(), *args, '--as','user','--format','json'],
        env=dict(os.environ,LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1'),
        capture_output=True,text=True,timeout=120)
    if result.returncode:
        raise RuntimeError('飞书读取失败，请检查服务账号授权和网络；原快照已保留')
    obj = json.loads(result.stdout)
    if obj.get('ok') is not True:
        raise RuntimeError('lark-cli did not confirm read success')
    return obj['data']

def now(): return datetime.now(timezone.utc).isoformat()
def order(item): return [int(v) if v.isdigit() else v for v in re.split(r'(\d+)', '/'.join(item['path']))]
def column(number):
    result = ''
    while number:
        number, rem = divmod(number-1,26)
        result = chr(65+rem)+result
    return result

def folder_children(folder):
    rows=[]; page=None; pages=set()
    while True:
        params={'folder_token':folder['token'],'page_size':200}
        if page: params['page_token']=page
        data=call('drive','files','list','--params',json.dumps(params))
        files=data.get('files')
        if not isinstance(files,list):raise RuntimeError('飞书目录读取结果缺少文件列表；原快照已保留')
        rows.extend(files)
        if not data.get('has_more'):return rows
        page=data.get('next_page_token')
        if not page or page in pages:raise RuntimeError('Incomplete folder pagination')
        pages.add(page)

def read_document(doc):
    meta=call('sheets','+workbook-info','--spreadsheet-token',doc['token'])
    revision=meta.get('revision'); sheets=[]
    for sheet in meta['sheets']:
        sid=sheet['sheet_id']; nr=sheet['row_count']; nc=sheet['column_count']
        if nr > 50000 or nc > 1000:raise RuntimeError('Sheet dimensions exceed snapshot limit')
        ranges=[]
        for start in range(1,nr+1,100):
            end=min(nr,start+99); source_range=f'A{start}:{column(nc)}{end}'
            data=call('sheets','+cells-get','--spreadsheet-token',doc['token'],
                '--sheet-id',sid,'--range',source_range,'--include','value,formula')
            if data.get('has_more') or not data.get('ranges'):
                raise RuntimeError(f'Incomplete range: {doc["name"]}/{sid}/{source_range}')
            if revision is not None and data.get('revision') != revision:
                raise RuntimeError(f'Sheet changed during read: {doc["name"]}; rerun snapshot')
            received=[]
            for block in data['ranges']:
                if block.get('truncated'): raise RuntimeError('Truncated cells')
                received.extend(block['row_indices'])
                if block['col_indices'] != [column(c) for c in range(1,nc+1)]:
                    raise RuntimeError('Incomplete columns')
                if len(block['cells']) != len(block['row_indices']) or any(len(row)!=nc for row in block['cells']):
                    raise RuntimeError('Cell dimensions do not match coordinates')
                ranges.append({k:block[k] for k in ('range','actual_range','row_indices','col_indices','cells','truncated') if k in block})
            if received != list(range(start,end+1)):raise RuntimeError('Incomplete row coordinates')
        sheets.append({k:sheet[k] for k in ('sheet_id','sheet_name','row_count','column_count','is_hidden') if k in sheet} | {'ranges':ranges})
    after=call('sheets','+workbook-info','--spreadsheet-token',doc['token'])
    if revision is not None and after.get('revision') != revision:
        raise RuntimeError('清单读取期间发生变化，请重试；原快照已保留')
    # Metadata title is authoritative if the file was renamed during folder listing.
    return doc | {'name':meta.get('title',doc['name']), 'revision':revision,'read_at':now(),'sheets':sheets}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--workers',type=int,default=3,choices=range(1,5))
    parser.add_argument('--directory-only',action='store_true',help='只刷新目录和清单链接，沿用旧单元格缓存，不读取表格内容')
    args=parser.parse_args()
    output=args.output.resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.with_suffix('.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError: raise RuntimeError('另一个清单同步任务正在运行') from None
        status_path=output.with_name('feishu_catalog_sync.json')
        try:
            if args.directory_only:
                publish_directory(args)
                state={'status':'success','checked_at':now(),'message':'只读目录同步完成；表格单元格沿用上次缓存，未实时读取清单内容'}
            else:
                publish(args)
                state={'status':'success','checked_at':now(),'message':'只读清单全量同步完成'}
        except Exception:
            state={'status':'failed','checked_at':now(),'message':'目录同步失败，保留上次成功快照；请检查飞书授权、网络和清单结构'}
            atomic_status(status_path,state)
            raise
        atomic_status(status_path,state)


def atomic_status(path, state):
    atomic_json(path,state,indent=None)

def atomic_json(path, payload, *, indent=2):
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,prefix=path.name+'.',suffix='.tmp',delete=False)
    temp=Path(handle.name)
    try:
        with handle:
            handle.write(json.dumps(payload,ensure_ascii=False,indent=indent)+'\n')
        temp.replace(path)
    except Exception:
        try:temp.unlink()
        except OSError:pass
        raise



def publish_directory(args):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from backend.catalog_refresh import LarkCatalogDirectoryReader, build_directory_snapshot
    from backend.feishu_catalog import load_snapshot
    old=None
    try:
        old=load_snapshot(args.output)
    except Exception:
        old=None
    snapshot=build_directory_snapshot(LarkCatalogDirectoryReader(command=lark_cli_command()), old)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    atomic_json(args.output,snapshot)
    print(json.dumps({'folders':len(snapshot['folders']),'documents':len(snapshot['documents']),'sheets':'cached','directory_only':True,'output':str(args.output),'cloud_writes':0},ensure_ascii=False),flush=True)

def publish(args):
    root={'token':ROOT_TOKEN,'parent_token':None,'name':'DMR','path':['DMR']}
    folders=[root]; queue=[root]; seen={ROOT_TOKEN}; docs=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        while queue:
            batch=queue;queue=[]
            for parent, children in zip(batch,pool.map(folder_children,batch)):
                for item in children:
                    if item['token'] in seen:raise RuntimeError('Duplicate/cyclic token in source tree')
                    seen.add(item['token'])
                    base={'token':item['token'],'parent_token':parent['token'],'name':item['name'],'path':parent['path']+[item['name']]}
                    if item['type']=='folder':folders.append(base);queue.append(base)
                    elif item['type']=='sheet':docs.append(base|{'type':'sheet','url':HOST+'/sheets/'+item['token']})
                    else:raise RuntimeError(f'Unsupported source type {item["type"]}: {item["name"]}')
            print(f'Discovered {len(folders)} folders, {len(docs)} documents',flush=True)
        documents=[]
        for i, doc in enumerate(pool.map(read_document,sorted(docs,key=order)),1):
            documents.append(doc)
            print(f'Read {i}/{len(docs)}: {doc["name"]} ({len(doc["sheets"])} sheets)',flush=True)
    snapshot={'schema_version':1,'root_token':ROOT_TOKEN,'root_url':HOST+'/drive/folder/'+ROOT_TOKEN,
        'fetched_at':now(),'writeback_enabled':False,'folders':sorted(folders,key=order),'documents':documents}
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from backend.feishu_catalog import _validate_snapshot
    _validate_snapshot(snapshot)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    atomic_json(args.output,snapshot)
    print(json.dumps({'folders':len(folders),'documents':len(documents),'sheets':sum(len(d['sheets']) for d in documents),'output':str(args.output),'cloud_writes':0},ensure_ascii=False),flush=True)
if __name__=='__main__':
    try: main()
    except Exception as exc:
        print('清单只读同步失败：'+str(exc),file=sys.stderr);sys.exit(1)
