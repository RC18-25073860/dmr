#!/usr/bin/env python3
"""Read-only release checks; never prints secret values or contacts external services."""
import argparse,json,os,shutil,sqlite3,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def check(root=ROOT):
    root=Path(root); data=root/os.environ.get('DMR_DATA_DIR','data'); problems=[]
    for f in ['requirements.lock.txt','frontend/dist/index.html','backend/catalog_locator.py','scripts/mail_sync.py']:
        if not (root/f).is_file(): problems.append('缺少发布文件：'+f)
    if not (data/'dmr.sqlite3').exists(): problems.append('尚未初始化业务数据库')
    if not (data/'feishu_catalog.json').exists(): problems.append('尚未生成飞书目录快照')
    if not shutil.which('lark-cli'): problems.append('缺少lark-cli，云端定位不可用')
    if os.environ.get('DMR_ENV') not in {'production','prod'}: problems.append('尚未启用生产环境配置')
    if os.environ.get('DMR_SETUP_ALLOWED','').lower()!='false':problems.append('生产必须关闭首次管理员初始化')
    if not os.environ.get('DMR_PUBLIC_ORIGIN','').startswith('https://'):problems.append('尚未配置HTTPS公开地址')
    if not os.environ.get('DMR_NAS_CREDENTIALS_KEY') or 'REPLACE' in os.environ['DMR_NAS_CREDENTIALS_KEY']:problems.append('NAS凭据加密密钥未配置')
    if not os.environ.get('DMR_NAS_SYNC_TOKEN') or 'REPLACE' in os.environ['DMR_NAS_SYNC_TOKEN']:problems.append('NAS worker鉴权令牌未配置')
    if not os.environ.get('DMR_NAS_READ_BASE_URL','').startswith('https://'):problems.append('NAS HTTPS下载服务地址未配置')
    return {'ready':not problems,'checks':problems,'unverified':['飞书服务身份和文档权限','真实192目录、写入、失败重试','HTTPS证书和反向代理','定时任务执行及告警','目标并发负载及恢复演练']}

if __name__=='__main__':
    r=check();print(json.dumps(r,ensure_ascii=False,indent=2));sys.exit(0 if r['ready'] else 1)
