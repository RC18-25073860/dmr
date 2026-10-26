"""Apply documented category rules without touching original files or archived versions."""
import argparse,collections,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.service import DMRStore,json_loads,json_dumps,now_iso
from backend.classification import apply_classification

def run(data_dir,apply=False):
    store=DMRStore(data_dir)
    changes=[]
    try:
        with store.lock,store.conn.immediate():
            rows=[dict(r) for r in store.execute('SELECT * FROM records').fetchall()]
            proposals={r['id']:apply_classification(r,r['file_name']) for r in rows}
            groups=collections.defaultdict(list)
            for r in rows:groups[(r['registrant'],r['product'])].append(r)
            for group in groups.values():
                categories={proposals[r['id']]['project_category'] for r in group}-{'待确认',''}
                if len(categories)>1:
                    for row in group:
                        item=proposals[row['id']]
                        if '人工' in (row['project_category_source'] or ''):continue
                        item['project_category']='待确认';item['project_category_source']='规则冲突：同主体同产品资料分类不同，需文控核对'
                        item['metadata'].setdefault('project_classification',{})['conflict']=True
                elif len(categories)==1:
                    for row in group:
                        item=proposals[row['id']]
                        if item['project_category']=='待确认' and not item['metadata'].get('project_classification',{}).get('conflict') and '人工' not in (row['project_category_source'] or ''):
                            item['project_category']=next(iter(categories));item['project_category_source']='继承同主体同产品已确认类别'
            for row in rows:
                item=proposals[row['id']]
                if '人工' in (row['project_category_source'] or ''):continue
                if (row['project_category'],row['project_category_source'],json_loads(row['metadata'],{}))==(item['project_category'],item['project_category_source'],item['metadata']):continue
                changes.append({'table':'records','id':row['id'],'code':row['business_code'],'product':row['product'],'before':row['project_category'],'after':item['project_category'],'source':item['project_category_source']})
                if apply:store.execute('UPDATE records SET project_category=?,project_category_source=?,metadata=?,edit_revision=edit_revision+1,updated_at=? WHERE id=?',(item['project_category'],item['project_category_source'],json_dumps(item['metadata']),now_iso(),row['id']))
            for raw in store.execute("SELECT * FROM batch_items WHERE status!='committed'").fetchall():
                row=dict(raw)
                if '人工' in (row['project_category_source'] or ''):continue
                item=store.inherit_project_category(row)
                # Keep pending batch lifecycle; never commit files as part of migration.
                status,record_id,recommendation,current=store.classify_item(item,row['sha256'])
                changes.append({'table':'batch_items','id':row['id'],'code':row['code'],'product':row['product'],'before':row['project_category'],'after':item['project_category'],'source':item['project_category_source'],'status':status})
                if apply:store.execute('UPDATE batch_items SET project_category=?,project_category_source=?,metadata=?,status=?,recommendation=?,edit_revision=edit_revision+1,updated_at=? WHERE id=?',(item['project_category'],item['project_category_source'],json_dumps(item['metadata']),status,recommendation,now_iso(),row['id']))
            if apply:
                store.recompute_link_statuses()
                if changes:store.add_log('自动分类规则更新','已有资料',f'按用户提供产品目录和BOM规则重算{len(changes)}项；保留人工确认，不修改原件及版本。','CLASSIFICATION-20260922','成功')
            counts=collections.Counter(p['project_category'] for p in proposals.values())
            return {'applied':apply,'record_categories':dict(counts),'changes':changes}
    finally:store.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data-dir',required=True);p.add_argument('--apply',action='store_true');p.add_argument('--output',required=True);a=p.parse_args()
    data=Path(a.data_dir).resolve();out=Path(a.output).resolve();data.relative_to(ROOT);out.relative_to(ROOT)
    result=run(data,a.apply);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({'applied':result['applied'],'record_categories':result['record_categories'],'changes':len(result['changes'])},ensure_ascii=False))
