import unittest
from backend.catalog_locator import _locate_in_rows

class RegionTests(unittest.TestCase):
    def rows(self):
        return {2:{'A':'深圳由莱 国内医疗版','H':'深圳由莱 海外医疗版'},7:{'A':'地区','C':'中国','H':'地区','J':'欧盟'},8:{'A':'序号','B':'名称','D':'编号','E':'版本','F':'备注','H':'序号','I':'名称','K':'编号','L':'版本','M':'备注'},13:{'D':'SOP-001','E':'A1','K':'SOP-001','L':'A2'}}
    def locate(self,rows):return _locate_in_rows(rows,'SOP-001','sheet','清单','now')
    def test_parallel_tables_each_have_their_own_version_and_region(self):
        matches,errors=self.locate(self.rows())
        self.assertFalse(errors)
        self.assertEqual([(m['code_cell'],m['version_cell'],m['current_version']) for m in matches],[('D13','E13','A1'),('K13','L13','A2')])
        self.assertEqual([m['region_area'] for m in matches],['中国','欧盟'])
        self.assertIn('海外',matches[1]['region_title'])
        self.assertEqual(matches[1]['region_end_column'],'M')
    def test_broken_left_region_does_not_steal_right_version(self):
        rows=self.rows();del rows[8]['E']
        matches,errors=self.locate(rows)
        self.assertTrue(errors)
        self.assertEqual([m['version_cell'] for m in matches],['L13'])
    def test_duplicate_version_headers_within_region_still_rejected(self):
        rows=self.rows();rows[8]['F']='版本号'
        matches,errors=self.locate(rows)
        self.assertTrue(errors)
        self.assertEqual([m['version_cell'] for m in matches],['L13'])
    def test_repeated_header_on_left_preserves_right_table(self):
        rows=self.rows();rows[12]={'A':'序号','B':'名称','D':'编号','E':'版本','F':'备注'}
        matches,errors=self.locate(rows)
        self.assertFalse(errors)
        self.assertEqual([m['version_cell'] for m in matches],['E13','L13'])
    def test_code_only_in_right_table_does_not_pair_left_version(self):
        rows=self.rows();rows[13]['D']='SOP-OTHER'
        matches,errors=self.locate(rows)
        self.assertFalse(errors);self.assertEqual(len(matches),1);self.assertEqual(matches[0]['version_cell'],'L13')
    def test_six_regions_and_two_letter_columns(self):
        rows={8:{'D':'编号','E':'版本','K':'编号','L':'版本','R':'编号','S':'版本','Y':'编号','Z':'版本','AF':'编号','AG':'版本','AM':'编号','AN':'版本'},20:{'AF':'SOP-001','AG':'A3','AM':'SOP-001','AN':'A4'}}
        matches,errors=self.locate(rows)
        self.assertFalse(errors);self.assertEqual([m['version_cell'] for m in matches],['AG20','AN20'])
