import unittest
from backend.service import DMRStore

class CandidateMessageTests(unittest.TestCase):
    def test_multiple_versions_are_reported_as_read_but_unconfirmed(self):
        store=object.__new__(DMRStore)
        result=store.classify_item({'code':'SOFT','registrant':'广西由莱','product':'UI20S','module':'软件','version':'','metadata':{'version_candidates':[{'version':'V1.0'},{'version':'V2.0'},{'version':'V1.0'}]}},'sha')
        self.assertEqual(result[0],'unmatched')
        self.assertIn('原件已读取到版本：V1.0、V2.0',result[2])
        self.assertIn('未自动取最大值',result[2])

    def test_no_evidence_does_not_claim_versions_were_read(self):
        result=object.__new__(DMRStore).classify_item({'metadata':{}},'sha')
        self.assertEqual(result[0],'unmatched')
        self.assertNotIn('原件已读取到版本',result[2])
