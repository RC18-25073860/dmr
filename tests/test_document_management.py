import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.document_management import catalog_update_policy


def policy(module, management_kind, *, kind='bom', name='sample.xlsx'):
    return catalog_update_policy(
        {'business_code': 'MAT-001', 'module': module, 'metadata': {'kind': kind, 'management_kind': management_kind}},
        {'name': name, 'metadata': {'kind': kind, 'management_kind': management_kind}},
    )


def test_specific_packaging_metadata_beats_generic_bom_module():
    result = policy('BOM', 'packaging_drawing')
    assert result['management_target'] == 'packaging_drawing_register'
    assert result['direct_catalog_row'] is False
    assert result['catalog_locator_enabled'] is False
    assert result['update_kind'] == 'register_review'
    assert '包装图纸台账' in result['catalog_location_hint']


def test_specific_incoming_metadata_does_not_depend_on_filename():
    result = policy('BOM', 'incoming_inspection', name='普通文件.xlsx')
    assert result['management_target'] == 'general_document_register'
    assert result['direct_catalog_row'] is False
    assert result['update_kind'] == 'general_register_review'


def test_ecn_and_hardware_are_manual_registers_not_dmr_rows():
    ecn = policy('BOM', 'ecn')
    hardware = policy('软件', 'hardware', kind='hardware')
    assert ecn['management_target'] == 'ecn_register'
    assert ecn['catalog_locator_enabled'] is False
    assert 'ECN' in ecn['management_target_label']
    assert hardware['management_target'] == 'hardware_register'
    assert hardware['catalog_locator_enabled'] is False


def test_software_release_keeps_release_instruction():
    result = policy('软件', 'release', kind='release')
    assert result['management_target'] == 'software_release_register'
    assert result['update_kind'] == 'software_release_review'
    assert '发布资料' in result['management_instruction']
    assert '软件附件' not in result['management_instruction']
