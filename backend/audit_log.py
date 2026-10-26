"""Bounded audit-log queries; archive rows are never deleted by pagination."""
from fastapi import HTTPException, Query, Request
from datetime import date


def install_audit_log(app, store, policy):
    @app.get('/api/logs')
    def logs(request: Request, q: str = Query('', max_length=240), user: str = 'all',
             action: str = 'all', scope: str = 'all', limit: int = Query(100, ge=1, le=200),
             offset: int = Query(0, ge=0)):
        with store.lock:
            policy(request, 'audit')
            if scope not in {'all', 'sync', 'manual', 'system'}:
                raise HTTPException(400, '日志范围无效')
            terms, params = [], []
            for field, value in [('user', user), ('action', action)]:
                if value != 'all':
                    terms.append(f'{field}=?'); params.append(value)
            if scope == 'sync': terms.append("action='同步数据'")
            elif scope == 'manual': terms.append("user!='系统自动任务'")
            elif scope == 'system': terms.append("user='系统自动任务'")
            for word in q.split():
                terms.append("instr(lower(coalesce(time,'')||' '||coalesce(user,'')||' '||coalesce(action,'')||' '||coalesce(object,'')||' '||coalesce(change,'')||' '||coalesce(source,'')||' '||coalesce(result,'')),lower(?))>0")
                params.append(word)
            where = (' WHERE ' + ' AND '.join(terms)) if terms else ''
            total = store.execute('SELECT COUNT(*) AS n FROM logs'+where, params).fetchone()['n']
            rows = store.execute('SELECT time,user,action,object,change,source,result FROM logs'+where+' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', params+[limit,offset]).fetchall()
            stats = dict(store.execute("SELECT COUNT(*) AS total,SUM(CASE WHEN substr(time,1,10)=? THEN 1 ELSE 0 END) AS today,SUM(CASE WHEN action='同步数据' THEN 1 ELSE 0 END) AS sync,SUM(CASE WHEN user!='系统自动任务' THEN 1 ELSE 0 END) AS manual FROM logs", (date.today().isoformat(),)).fetchone())
            return {'items':[dict(r) for r in rows], 'total':total,'offset':offset,'limit':limit,
                    'has_more':offset+len(rows)<total,'stats':stats,
                    'users':[r['user'] for r in store.execute('SELECT DISTINCT user FROM logs ORDER BY user').fetchall()],
                    'actions':[r['action'] for r in store.execute('SELECT DISTINCT action FROM logs ORDER BY action').fetchall()]}
