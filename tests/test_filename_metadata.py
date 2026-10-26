import pytest
from parsers import parse_file, enrich_batch
from filename_metadata import enrich_filename
from test_product_identity import docx

@pytest.mark.parametrize('module,word', [('BOM','BOM'),('软件','软件'),('工艺文件','作业指导书'),('检验文件','检验规范'),('承认书&规格书','规格书'),('包装图纸','包装图'),('结构图纸','结构图')])
def test_all_modules_filename_fields(module,word):
    item=parse_file(b'attachment',f'SOP-03-009_UI20S_{word}_V1.2_【广西由莱】.txt')
    assert (item['code'],item['product'],item['registrant'],item['version'],item['module']) == ('SOP-03-009','UI20S','广西由莱','V1.2',module)


def test_body_version_conflict():
    item=parse_file(docx('软件发布记录 发布版本：V2.0'), 'UI20S_软件_V1.0_【广西由莱】.docx')
    assert item['version']==''
    assert item['metadata']['version_reading_status']=='pending'


def test_subject_conflict_and_missing():
    item=parse_file(b'', 'UI20S_软件_V1.0_【广西由莱】【深圳由莱】.hex')
    assert item['registrant']==''
    item=parse_file(b'', 'UI20S_软件_V1.0_【测试附件】.hex')
    assert item['registrant']==''


def test_revision_and_unknown_extension():
    item=parse_file(b'', 'SOP-03-009_UI20S_结构图_A1_【深圳由莱】.dwg')
    assert item['version']=='A1'
    assert item['module']=='结构图纸'


def test_date_rule_preserved():
    item={'metadata':{'version_rule':'filename_date'}, 'notes':[], 'version':'20260928'}
    assert enrich_filename(item,'UI20S_BOM_V1.0_20260928.xlsx')['version']=='20260928'


def test_batch_cannot_silently_override_filename_subject():
    name='UI20S_软件_V1.0_【广西由莱】.hex'
    item=parse_file(b'',name)
    history={'metadata':{'kind':'software_history','versions':[{'name':name,'registrant':'深圳由莱','version':'V1.0','product':'UI20S','linked_bom':'BOM1'}]}}
    enrich_batch([history,item])
    assert item['registrant']==''
    assert item['metadata']['registrant_reading_status']=='pending'


def test_packaging_drawing_filename_keeps_material_date_and_version_separate():
    item=parse_file(b'not-a-pdf','7322200005-深圳Ulike-JSG10-电极头保护膜-转曲-20250908-V0.pdf')
    assert item['metadata']['management_kind']=='packaging_drawing'
    assert item['code']=='7322200005'
    assert item['version']=='V0'
    assert item['registrant']=='深圳Ulike'
    assert item['metadata']['identifiers']['material_no']=='7322200005'
    assert item['metadata']['dates']['drawing_date']=='20250908'


def test_structural_drawing_filename_keeps_effective_date_separate_from_version():
    item=parse_file(b'not-a-pdf','【深圳由莱】 123456A 零件图 V1.1 20241009.pdf')
    assert item['metadata']['management_kind']=='structural_drawing'
    assert item['code']=='123456A'
    assert item['version']=='V1.1'
    assert item['metadata']['identifiers']['drawing_no']=='123456A'
    assert item['metadata']['dates']['effective_date']=='20241009'


def test_acknowledgement_without_version_is_not_defaulted_to_v0():
    item=parse_file(b'not-a-pdf','1234567890 电极头保护膜 承认书.pdf')
    assert item['metadata']['management_kind']=='acknowledgement'
    assert item['code']=='1234567890'
    assert item['version']==''
    assert item['metadata']['identifiers']['file_no']=='1234567890'


def test_ecn_filename_records_subject_number_without_document_category_guessing():
    item=parse_file(b'not-a-zip','2026090001_UI20S_ECN.xlsx')
    assert item['metadata']['management_kind']=='ecn'
    assert item['metadata']['identifiers']['ecn_no']=='2026090001'
    assert item['metadata']['dates']['ecn_month']=='202609'
    assert item['product']=='UI20S'


def test_software_filename_branch_and_program_version_are_explicit_evidence():
    item=parse_file(b':020000040000FA','UI20S_CH_JZ_V1.0.2.00.hex')
    assert item['metadata']['management_kind']=='software'
    assert item['metadata']['version_semantic']=='software_program_version'
    assert item['metadata']['software_branch']=='UI20S_CH_JZ'
    assert item['metadata']['identifiers']['software_branch']=='UI20S_CH_JZ'
    assert item['metadata']['identifiers']['software_version']=='V1.0.2.00'


def test_bom_filename_profile_survives_unreadable_workbook_for_manual_review():
    item=parse_file(b'not-a-zip','1106S05A01B_V1.0-UI06S PR【广西由莱】-成品-20260702.xlsx')
    assert item['metadata']['management_kind']=='bom'
    assert item['metadata']['identifiers']['bom_no']=='1106S05A01B'
    assert item['metadata']['identifiers']['document_version']=='V1.0'
    assert item['metadata']['dates']['version_date']=='20260702'


def test_bom_distribution_pdf_is_notice_not_software_or_bom_workbook():
    item = parse_file(b'not-a-pdf', 'BOM发放（W20冷凝舱_5弹丽驻兰礼盒_新国标 成品）.pdf')
    assert item['module'] == 'BOM'
    assert item['type'] == 'BOM发放通知'
    assert item['metadata']['management_kind'] == 'bom_notice'
    assert item['metadata']['document_role'] == 'bom_release_notice'
    assert item['version'] == ''


def test_incoming_inspection_is_not_routed_as_production_dmr_row():
    item=parse_file(b'not-a-zip','来料检验_UI20S_V1.0.xlsx')
    assert item['metadata']['management_kind']=='incoming_inspection'
    assert item['module']=='检验文件'
    assert item['product']=='UI20S'


def test_release_table_filename_is_release_document_role():
    item=parse_file(docx('软件发布表 发布版本：V2.0'), 'UI20S_CH_JZ软件发布表.docx')
    assert item['metadata']['management_kind']=='release'
    assert item['metadata']['document_role']=='software_release_record'
    assert item['type']=='软件发布记录'


def test_trial_flag_requires_explicit_trial_or_9_prefix_not_generic_test_words():
    assert parse_file(b'', '1106S05A01B_V1.0-UI06S【广西由莱】-成品-20260702.xlsx')['metadata'].get('trial_flag') is None
    assert parse_file(b'', '1106S05A01B_V1.0-UI06S【广西由莱】-试产-20260702.xlsx')['metadata']['trial_flag'] is True
    assert parse_file(b'', '9106S05A01B_V1.0-UI06S【广西由莱】-成品-20260702.xlsx')['metadata']['trial_flag'] is True


def test_packaging_profile_overrides_generic_bom_or_software_defaults():
    item={'module':'BOM','metadata':{'kind':'bom','management_kind':'bom'},'notes':[]}
    enrich_filename(item,'7322200005-深圳Ulike-JSG10-电极头保护膜-转曲-20250908-V0.pdf')
    assert item['metadata']['management_kind']=='packaging_drawing'
    assert item['registrant']=='深圳Ulike'


def test_calibration_tool_is_software_not_hardware():
    item=parse_file(b'zipdata','UI20S校准工具V1.19.04.zip')
    assert item['metadata']['management_kind']=='software'
    assert item['module']=='软件'
    assert item['type']=='工具包'
