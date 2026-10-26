import unittest
from backend.manual_catalog_updates import document_locations

class LocationTests(unittest.TestCase):
    def doc(self):
        return {'read_at':'2026-09-24', 'sheets':[{'sheet_name':'BOM清单','sheet_id':'s1','ranges':[{'row_indices':[8,38], 'col_indices':['B','D','E'], 'cells':[['名称','编号','版本'],['BOM','11UI04AA01','20260410']]}]}]}
    def test_sparse_coordinates(self):
        location=document_locations(self.doc())['11UI04AA01']
        self.assertEqual((location['row'],location['code_cell'],location['version_cell']), (38,'D38','E38'))
    def test_duplicate_code_not_guessed(self):
        doc=self.doc(); block=doc['sheets'][0]['ranges'][0]
        block['row_indices'].append(45);block['cells'].append(['BOM','11UI04AA01','20260924'])
        self.assertNotIn('11UI04AA01',document_locations(doc))
    def test_no_version_header_not_guessed(self):
        doc=self.doc();doc['sheets'][0]['ranges'][0]['cells'][0][-1]='备注'
        self.assertEqual(document_locations(doc),{})
    def test_nonstandard_column_and_header(self):
        doc=self.doc();block=doc['sheets'][0]['ranges'][0];block['col_indices']=['A','F','AA'];block['cells'][0]=['名称','文件编号','版本号']
        self.assertEqual(document_locations(doc)['11UI04AA01']['version_cell'],'AA38')
