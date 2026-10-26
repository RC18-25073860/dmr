import unittest

from test_permissions import PermissionsHarness


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.h = PermissionsHarness()
        self.admin = self.h.setup_admin()

    def tearDown(self):
        self.h.close()

    def log(self, suffix, *, user='管理员 [admin]', action='人工核对', obj=None,
            change=None, source='BATCH-1', result='成功', created_at=None):
        row_id = f'LOG-AUDIT-{suffix:03d}'
        self.h.store.execute(
            "INSERT INTO logs(id,time,user,action,object,change,source,result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (row_id, '2026-09-28', user, action, obj or f'对象-{suffix:03d}',
             change or f'普通变更 {suffix:03d}', source, result, created_at or f'2026-09-28T10:00:{suffix:02d}'),
        )
        return row_id

    def get_logs(self, client=None, **params):
        response = (client or self.admin).get('/api/logs', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_authentication_and_audit_permission_are_required(self):
        anonymous = self.h.client()
        self.assertEqual(anonymous.get('/api/logs').status_code, 401)

        _user, password = self.h.create_user(
            self.admin,
            'audit-reader',
            role='reader',
            scopes=[{'registrant': '*', 'project_category': '*', 'product': '*'}],
        )
        reader = self.h.login('audit-reader', password)
        self.assertEqual(reader.get('/api/logs').status_code, 403)

        _controller, controller_password = self.h.create_user(
            self.admin,
            'audit-controller',
            role='document_control',
            scopes=[{'registrant': '*', 'project_category': '*', 'product': '*'}],
        )
        controller = self.h.login('audit-controller', controller_password)
        self.log(1)
        self.assertEqual(controller.get('/api/logs').status_code, 200)

    def test_pagination_is_stable_and_has_no_duplicate_rows(self):
        for index in range(8):
            self.log(index, created_at='2026-09-28T10:00:00')

        first = self.get_logs(limit=3, offset=0)
        second = self.get_logs(limit=3, offset=3)

        first_objects = [row['object'] for row in first['items']]
        second_objects = [row['object'] for row in second['items']]
        self.assertEqual(len(first_objects), 3)
        self.assertEqual(len(second_objects), 3)
        self.assertFalse(set(first_objects) & set(second_objects))
        self.assertTrue(first['has_more'])
        self.assertEqual(first['total'], 8)

    def test_keyword_search_scans_beyond_first_unfiltered_page(self):
        for index in range(12):
            self.log(index, change=f'普通日志 {index}', created_at=f'2026-09-28T10:00:{index:02d}')
        self.log(50, obj='远端清单', change='包含跨页关键字-needle', created_at='2026-09-28T09:00:00')

        unfiltered = self.get_logs(limit=5)
        self.assertNotIn('远端清单', [row['object'] for row in unfiltered['items']])

        found = self.get_logs(q='needle', limit=5)
        self.assertEqual(found['total'], 1)
        self.assertEqual(found['items'][0]['object'], '远端清单')

    def test_user_action_and_scope_filters(self):
        self.log(1, user='管理员 [admin]', action='人工核对', obj='人工项')
        self.log(2, user='系统自动任务', action='同步数据', obj='同步项')
        self.log(3, user='文控 [doc]', action='清单人工确认', obj='清单项')
        self.log(4, user='系统自动任务', action='巡检', obj='系统项')

        self.assertEqual([row['object'] for row in self.get_logs(user='文控 [doc]')['items']], ['清单项'])
        self.assertEqual([row['object'] for row in self.get_logs(action='同步数据')['items']], ['同步项'])
        self.assertEqual([row['object'] for row in self.get_logs(scope='sync')['items']], ['同步项'])
        manual_objects = {row['object'] for row in self.get_logs(scope='manual')['items']}
        self.assertEqual(manual_objects, {'人工项', '清单项'})
        system_objects = {row['object'] for row in self.get_logs(scope='system')['items']}
        self.assertEqual(system_objects, {'同步项', '系统项'})

        invalid = self.admin.get('/api/logs', params={'scope': 'unknown'})
        self.assertEqual(invalid.status_code, 400)


if __name__ == '__main__':
    unittest.main()
