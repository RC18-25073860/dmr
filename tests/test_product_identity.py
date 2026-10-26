from io import BytesIO
import zipfile
from xml.sax.saxutils import escape
from parsers import parse_file, enrich_batch
from product_identity import models, resolve_product


def docx(text):
    out=BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+escape(text)+'</w:t></w:r></w:p></w:body></w:document>')
    return out.getvalue()


def test_filename_model_when_body_missing():
    item=parse_file(docx('软件发布记录 发布版本: V1.0'), 'UI20S_CH_JZ软件发布记录.docx')
    assert item['product']=='UI20S'
    assert item['metadata']['product_source']=='文件名'
    assert any('按文件名识别' in note for note in item['notes'])


def test_body_only_and_matching_filename():
    for name in ['软件发布记录.docx','UI20S_软件发布记录.docx']:
        item=parse_file(docx('型号：UI20S GR 软件版本: V1.0'),name)
        assert item['product']=='UI20S GR'
        assert item['metadata']['product_source']=='正文'


def test_conflict_does_not_select():
    item=parse_file(docx('型号：UI04 软件版本: V1.0'), 'UI20S_软件发布记录.docx')
    assert item['product']==''
    assert item['metadata']['product_reading_status']=='pending'
    assert {e['product'] for e in item['metadata']['product_candidates']}=={'UI04','UI20S'}


def test_multiple_filename_models_remain_pending():
    item=parse_file(b'firmware','UI20&UI20S_V1.0.hex')
    assert item['product']==''
    assert item['metadata']['product_reading_status']=='pending'


def test_no_chip_version_or_embedded_bom_guess():
    assert models('11UI04AA01_V1.0_PY32F031_IC_20260928')==[]
    assert parse_file(b'','CH_JZ_V1.0.hex')['product']==''


def test_filename_prefix_and_suffix_and_case():
    for name in ['ui20s_CH_JZ_V1.0.hex','SOP-03-001_UI20S_检验规范.pdf']:
        item=resolve_product({'product':'','metadata':{},'notes':[]},name)
        assert item['product']=='UI20S'
    assert models('UI04_UI04')==['UI04']


def test_batch_enrichment_cannot_hide_model_conflict():
    item=parse_file(b'','UI20S_V1.0.hex')
    history={'metadata':{'kind':'software_history','versions':[{'name':'UI20S_V1.0.hex','product':'UI04','registrant':'广西由莱','linked_bom':'11UI04AA01','version':'V1.0'}]}}
    result=enrich_batch([history,item])[1]
    assert result['product']==''
    assert result['metadata']['product_reading_status']=='pending'


def test_ui_generation_tags_are_not_product_models():
    for name in ['UI20S_G2(G3_HardWare)_TOUCH_2KEY_V1.0.0.00.socprj', 'UI20S_G2(G3_HardWare)_Config_IC=PY32F031xx8_20260106.V16PY32Config', 'UI20S_G3硬件转UI20S_G2国内软件.xlsx']:
        item=resolve_product({'product':'','metadata':{},'notes':[]},name)
        assert item['product']=='UI20S'
        resolve_product(item,name)
        assert len([n for n in item['notes'] if '按文件名识别' in n or n.startswith('型号来源')])==1
    assert models('G18_BOM.xlsx')==['G18']
    assert models('G20_BOM.xlsx')==['G20']
    assert models('UI20S&G2')==['UI20S','G2']
