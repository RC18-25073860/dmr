from backend.catalog_locator import _locate_in_rows, stable_name


def rows():
    return {1:{'A':'序号','B':'名称','D':'编号','E':'版本'},2:{'A':'软件'},3:{'A':'1','B':'UI20S_CH_JZ_V1.0.hex','D':'/','E':'V1.0'},4:{'A':'BOM'},5:{'A':'1','B':'UI20S_CH_JZ_V1.0.hex','D':'SOP-001','E':'V1.0'}}


def locate(code='',filename='',module='软件', data=None):
    return _locate_in_rows(data or rows(),code,'sheet','name','now',filename=filename,module=module)[0]


def test_exact_name_without_code():
    hit=locate(filename='UI20S_CH_JZ_V1.0.hex')[0]
    assert hit['row']==3 and hit['name_cell']=='B3' and hit['version_cell']=='E3'
    assert hit['match_basis']=='name'


def test_normalized_name_is_candidate():
    hit=locate(filename='UI20S_CH_JZ_V2.0.hex')[0]
    assert hit['match_basis']=='normalized_name'
    assert not locate(filename='UI04_CH_JZ_V2.0.hex')


def test_category_isolation():
    assert not locate('SOP-001')
    assert locate('SOP-001',module='BOM')[0]['row']==5


def test_duplicate_names_are_all_returned():
    data=rows();data[4]={'A':'2','B':'UI20S_CH_JZ_V1.0.hex','D':'/','E':'V1.0'}
    # The numbered row is also a name hit under the confirmed OR rule.
    assert len(locate(filename='UI20S_CH_JZ_V1.0.hex',data=data))==3


def test_name_only_header():
    data={1:{'A':'名称','B':'版本'},2:{'A':'软件'},3:{'A':'UI20S.hex','B':'V1.0'}}
    assert locate(filename='UI20S.hex',data=data)[0]['version_cell']=='B3'


def test_unknown_category_does_not_guess():
    data=rows();data[2]={'A':'未定义类别'}
    assert not locate(filename='UI20S_CH_JZ_V1.0.hex',data=data)


def test_normalization_keeps_model_and_variant():
    assert stable_name('UI20S_DB_V1.0_20260928.hex') != stable_name('UI20S_GR_V2.0.hex')


def test_seven_categories():
    for module in ['BOM','软件','工艺文件','检验文件','承认书&规格书','包装图纸','结构图纸']:
        data=rows();data[2]={'A':module}
        assert locate(filename='UI20S_CH_JZ_V1.0.hex',module=module,data=data)[0]['row']==3


def test_parallel_regions_preserve_distinct_name_cells():
    data={1:{'A':'名称','B':'编号','C':'版本','E':'名称','F':'编号','G':'版本'},2:{'A':'软件','E':'软件'},3:{'A':'UI20S.hex','B':'/','C':'V1.0','E':'UI20S.hex','F':'/','G':'V2.0'}}
    assert {x['version_cell'] for x in locate(filename='UI20S.hex',data=data)}=={'C3','G3'}


def test_changed_code_still_returns_name_candidate():
    data=rows(); data[3]['D']='OLD-001'
    hit=locate('NEW-002',filename='UI20S_CH_JZ_V2.0.hex',data=data)[0]
    assert hit['match_basis']=='name_changed_code'
    assert hit['current_code']=='OLD-001' and hit['name_cell']=='B3'


def test_keyword_name_search_with_numbered_rows():
    data=rows(); data[3].update(B='UI20S触摸校准工具',D='OLD-001')
    assert locate(filename='触摸校准',data=data)[0]['match_basis']=='keyword_name'
    assert not locate(filename='UI20S',data=data)


def test_code_or_name_returns_all_hits_not_only_code_hit():
    data=rows(); data[3]['D']='NEW-002'
    data.pop(5)
    data[4]={'A':'2','B':'UI20S_CH_JZ_V1.0.hex','D':'OLD-001','E':'V1.0'}
    hits=locate('NEW-002',filename='UI20S_CH_JZ_V2.0.hex',data=data)
    assert {h['row'] for h in hits}=={3,4}
    assert {h['match_basis'] for h in hits}=={'code','name_changed_code'}


def test_changed_number_prefix_and_version_are_candidate_only():
    data=rows(); data[3].update(B='OLD-001 UI20S触摸校准 V1.0.pdf',D='OLD-001')
    hit = locate('NEW-002',filename='NEW-002 UI20S触摸校准 V2.0.pdf',data=data)[0]
    assert hit['match_basis']=='name_changed_code'
    assert hit['requires_manual_confirmation'] is True
