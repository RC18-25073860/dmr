"""Shared filename evidence for all seven document modules."""
import re
from pathlib import Path

MODULES = {
    'BOM': ('BOM', '物料清单'),
    '软件': ('软件', '固件', '配置文件', '校准工具'),
    '工艺文件': ('工艺', '作业指导书'),
    '检验文件': ('检验', '检验规范'),
    '承认书&规格书': ('承认书', '规格书'),
    '包装图纸': ('包装图纸', '包装图', '彩盒图', '外箱图'),
    '结构图纸': ('结构图纸', '结构图', '零件图', '装配图'),
}
EMPTY = {'', '待确认', '未提供'}

MANAGEMENT_BY_MODULE = {
    'BOM': 'bom',
    '软件': 'software',
    '工艺文件': 'process',
    '检验文件': 'inspection',
    '承认书&规格书': 'acknowledgement',
    '包装图纸': 'packaging_drawing',
    '结构图纸': 'structural_drawing',
}

SOFTWARE_EXTENSIONS = ('.hex', '.socprj', '.bin', '.dfu', '.elf')


def infer_management_kind(item, filename):
    """Infer the checklist/ledger family without changing user-facing category fields."""
    name = Path(filename).name
    stem = Path(filename).stem
    meta = item.setdefault('metadata', {})
    haystack = stem.casefold()
    lower_name = name.casefold()
    module = str(item.get('module') or '')
    kind = str(meta.get('kind') or '')

    if name.lower().endswith('.pdf') and stem.startswith('BOM发放'):
        return 'bom_notice'

    if kind == 'bom' or module == 'BOM':
        return 'bom'
    if kind == 'software_history' or '履历' in stem:
        return 'history'
    if '发布记录' in stem or '发行记录' in stem or '发布表' in stem or '发行表' in stem:
        return 'release'
    if kind in {'firmware_hex', 'socprj', 'config'} or lower_name.endswith(SOFTWARE_EXTENSIONS) or 'v16py32config' in lower_name:
        return 'software'
    if kind == 'zip_attachment' and ('软件' in stem or '校准' in stem or '工具' in stem):
        return 'software'
    if re.search(r'(?:^|[_\-\s])ECN[_\-\s]?\d|(?:^|[_\-\s])20\d{4}[-_\s]?\d{4}(?:$|[_\-\s])', stem, re.I):
        return 'ecn'
    if '来料' in stem or '进料' in stem or '入厂' in stem or '进货' in stem:
        return 'incoming_inspection'
    if '结构' in stem or '零件图' in stem or '装配图' in stem:
        return 'structural_drawing'
    if '包装图' in stem or '包装图纸' in stem or '彩盒图' in stem or '外箱图' in stem or '-转曲-' in stem:
        return 'packaging_drawing'
    if '承认书' in stem:
        return 'acknowledgement'
    if '规格书' in stem:
        return 'specification'
    if '检验' in stem:
        return 'inspection'
    if '工艺' in stem or '作业指导书' in stem:
        return 'process'
    if module in MANAGEMENT_BY_MODULE:
        return MANAGEMENT_BY_MODULE[module]
    if '软件' in haystack or 'firmware' in haystack:
        return 'software'
    return meta.get('management_kind') or ''


def enrich_filename(item, filename):
    path = Path(filename)
    name = path.stem
    meta = item.setdefault('metadata', {})
    notes = item.setdefault('notes', [])
    meta.setdefault('field_evidence', [])
    meta.setdefault('identifiers', {})
    meta.setdefault('dates', {})
    # Only explicit company names/labels; arbitrary bracketed descriptions are not companies.
    subjects = re.findall(r'(?:深圳|广西|杭州|新加坡|越南)(?:由莱|金茉|金荣|实业)', name)
    subjects += re.findall(r'(?:主体|公司)[：:]\s*([^_【】（）()]+)', name)
    versions = re.findall(r'(?<![A-Za-z0-9])V\d+(?:\.\d+)*(?![A-Za-z0-9])', name, re.I)
    versions += re.findall(r'(?:版本|版次)[：:_ -]*([VAB]\d+(?:\.\d+)*)', name, re.I)
    versions += re.findall(r'(?:^|[_ -])([AB]\d+)(?=$|[_ -])', name, re.I)
    modules = [module for module, words in MODULES.items() if any(word.casefold() in name.casefold() for word in words)]
    code = re.match(r'^((?:[A-Z]{1,8}-){1,3}\d{2,}(?:-\d+)*|\d{2}[A-Z0-9]{6,})(?=[_\s-]|$)', name, re.I)
    evidence = {'registrant': list(dict.fromkeys(subjects)), 'version': list(dict.fromkeys(v.upper() for v in versions)), 'module': modules}
    if code:
        evidence['code'] = [code.group(1)]
    profile = _filename_profile(path)
    if profile['subjects']:
        evidence['registrant'] = list(dict.fromkeys(evidence.get('registrant', []) + profile['subjects']))
    if profile['versions']:
        evidence['version'] = list(dict.fromkeys(evidence.get('version', []) + profile['versions']))
    if profile['code']:
        evidence['code'] = list(dict.fromkeys(evidence.get('code', []) + [profile['code']]))
    meta['filename_fields'] = evidence
    meta['management_kind'] = infer_management_kind(item, filename)
    current_management = str(meta.get('management_kind') or '')
    if profile['management_kind'] and current_management in {'', 'unknown', 'spreadsheet', 'bom', 'software'}:
        meta['management_kind'] = profile['management_kind']
    if profile['identifiers']:
        meta['identifiers'].update({k: v for k, v in profile['identifiers'].items() if v})
    if profile['dates']:
        meta['dates'].update({k: v for k, v in profile['dates'].items() if v})
    if profile['document_role']:
        meta['document_role'] = profile['document_role']
    if profile['software_branch']:
        meta['software_branch'] = profile['software_branch']
        meta['identifiers'].setdefault('software_branch', profile['software_branch'])
    if profile['trial_flag']:
        meta['trial_flag'] = True
    meta['field_evidence'].extend(profile['field_evidence'])
    for field, values in evidence.items():
        if not values:
            continue
        # Finished-goods BOM uses its already-confirmed filename date rule.
        if field == 'version' and meta.get('version_rule') == 'filename_date':
            continue
        current = str(item.get(field) or '').strip()
        # Generic document parsers use the whole filename as a provisional identifier.
        if field == 'code' and (current in name or current == name) and current != values[0]:
            if meta.get('kind') in {'unknown', 'pdf', 'docx', 'spreadsheet'}:
                current = ''
        candidates = list(dict.fromkeys(([current] if current not in EMPTY else []) + values))
        pending_body = field == 'version' and not current and bool(meta.get('version_candidates'))
        if len(candidates) > 1 or pending_body:
            item[field] = ''
            meta[field + '_reading_status'] = 'pending'
            meta[field + '_filename_conflict'] = candidates
            message = {'registrant': '公司主体', 'version': '版本', 'module': '资料类别', 'code': '文件编号'}[field] + '存在多个候选或与原件冲突：' + '；'.join(candidates) + '，请人工确认。'
            if message not in notes:
                notes.append(message)
        else:
            item[field] = values[0] if current in EMPTY else current
            meta[field + '_source'] = '文件名' if current in EMPTY else '原件/已有解析与文件名一致'
            meta['field_evidence'].append({'field': field, 'source': 'filename', 'value': item[field]})
    return item


def _filename_profile(path):
    name = path.stem
    suffix = path.suffix.lower()
    subjects = re.findall(r'(?:深圳|广西|杭州|新加坡|越南)(?:由莱|金茉|金荣|实业)', name)
    versions = []
    code = ''
    identifiers = {}
    dates = {}
    field_evidence = []
    management_kind = ''
    document_role = ''
    software_branch = ''
    trial_flag = bool(re.search(r'试产', name) or re.match(r'9[A-Z0-9]{7,}(?:[_\-\s]|$)', name, re.I))

    for index, match in enumerate(_date_matches(name), start=1):
        value = match.group(0).replace('-', '').replace('.', '').replace('_', '')
        key = 'filename_date' if index == 1 else f'filename_date_{index}'
        dates[key] = value
        field_evidence.append({'field': key, 'source': 'filename', 'value': value})

    ecn = re.search(r'(?<!\d)(20\d{4})[-_\s]?(\d{4})(?!\d)', name)
    if ecn:
        management_kind = 'ecn'
        identifiers['ecn_no'] = ecn.group(1) + ecn.group(2)
        dates['ecn_month'] = ecn.group(1)
        field_evidence.append({'field': 'ecn_no', 'source': 'filename', 'value': identifiers['ecn_no']})

    packaging = re.match(r'(?P<material>\d{6,})-(?P<company>[^-]+)-(?P<title>.+?)-转曲-(?P<drawing_date>20\d{6})-(?P<version>V\d+(?:\.\d+)*)$', name, re.I)
    if packaging:
        management_kind = 'packaging_drawing'
        code = packaging.group('material')
        versions.append(packaging.group('version').upper())
        dates['drawing_date'] = packaging.group('drawing_date')
        subjects.append(packaging.group('company'))
        identifiers.update({'material_no': code, 'drawing_title': packaging.group('title')})
        field_evidence.extend([
            {'field': 'material_no', 'source': 'filename', 'value': code},
            {'field': 'drawing_date', 'source': 'filename', 'value': packaging.group('drawing_date')},
            {'field': 'version', 'source': 'filename', 'value': packaging.group('version').upper(), 'scope': 'packaging_drawing'},
        ])

    structural = re.match(r'【(?P<company>[^】]+)】\s*(?P<drawing_no>[A-Za-z0-9-]{6,})\s+(?P<title>.+?)\s+(?P<version>V\d+(?:\.\d+)*)\s+(?P<effective>20\d{6})$', name, re.I)
    if structural:
        management_kind = 'structural_drawing'
        code = structural.group('drawing_no')
        versions.append(structural.group('version').upper())
        subjects.append(structural.group('company'))
        dates['effective_date'] = structural.group('effective')
        identifiers.update({'drawing_no': code, 'drawing_title': structural.group('title')})
        field_evidence.extend([
            {'field': 'drawing_no', 'source': 'filename', 'value': code},
            {'field': 'effective_date', 'source': 'filename', 'value': structural.group('effective')},
            {'field': 'version', 'source': 'filename', 'value': structural.group('version').upper(), 'scope': 'structural_drawing'},
        ])

    if not code:
        prefix = re.match(r'^((?:[A-Z]{1,8}-){1,3}\d{2,}(?:-\d+)*|\d{2}[A-Z0-9]{6,})(?=[_\s-]|$)', name, re.I)
        if prefix:
            code = prefix.group(1)
            identifiers['file_no'] = code
            field_evidence.append({'field': 'file_no', 'source': 'filename', 'value': code})

    bom = re.match(r'(?P<code>\d{2}[A-Z0-9]{6,})_(?P<doc_version>V\d+(?:\.\d+)*)[-_](?P<title>.+?)(?:[-_](?P<date>20\d{6}))?$', name, re.I)
    if bom and ('成品' in name or 'BOM' in name.upper()):
        management_kind = 'bom'
        code = bom.group('code')
        versions.append(bom.group('doc_version').upper())
        identifiers.setdefault('bom_no', code)
        identifiers.setdefault('document_version', bom.group('doc_version').upper())
        if bom.group('date'):
            dates['version_date'] = bom.group('date')
        field_evidence.append({'field': 'bom_no', 'source': 'filename', 'value': code})

    software_match = re.search(r'(?P<branch>[A-Za-z0-9_&().=-]+?)[_\-.]?(?P<version>[VA]\d+(?:\.\d+){2,})(?=$|[_\-.])', name, re.I)
    if software_match and (suffix in SOFTWARE_EXTENSIONS or 'v16py32config' in path.name.casefold() or '软件' in name):
        management_kind = management_kind or 'software'
        software_branch = software_match.group('branch').strip(' _-.')
        versions.append(software_match.group('version').upper())
        if not code:
            code = software_branch
        identifiers['software_branch'] = software_branch
        identifiers['software_version'] = software_match.group('version').upper()
        field_evidence.extend([
            {'field': 'software_branch', 'source': 'filename', 'value': software_branch},
            {'field': 'version', 'source': 'filename', 'value': software_match.group('version').upper(), 'scope': 'software_file'},
        ])

    if '履历' in name:
        document_role = 'software_history'
        management_kind = 'history'
    elif '发布记录' in name or '发行记录' in name or '发布表' in name or '发行表' in name:
        document_role = 'software_release_record'
        management_kind = 'release'
    elif '承认书' in name:
        management_kind = management_kind or 'acknowledgement'
    elif '规格书' in name:
        management_kind = management_kind or 'specification'

    return {
        'subjects': list(dict.fromkeys(subjects)),
        'versions': list(dict.fromkeys(versions)),
        'code': code,
        'identifiers': identifiers,
        'dates': dates,
        'field_evidence': field_evidence,
        'management_kind': management_kind,
        'document_role': document_role,
        'software_branch': software_branch,
        'trial_flag': trial_flag,
    }


def _date_matches(text):
    return list(re.finditer(r'(?<!\d)20\d{2}[._-]?\d{2}[._-]?\d{2}(?!\d)', text))
