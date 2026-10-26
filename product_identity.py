"""Conservative product-model evidence from content and filenames; no alias guessing."""
import re
from pathlib import Path

# Product families already used in this library. Firmware versions, BOM codes,
# chip identifiers and unconfirmed abbreviations are deliberately not models.
MODEL = re.compile(r'(?<![A-Za-z0-9])((?:UI|UIM|UM|JI|JD|JSG|JCS|EM|EG|UC|UN|UD|UB|JM|MI|W|G|M)\d{1,3}[A-Z]{0,2}(?:PRO(?:MAX)?)?)(?![A-Za-z0-9])', re.I)
VARIANT = re.compile(r'^[ _-]+(PRO(?:MAX)?|GR|DB|RE|PW|GP|WH|MP|BL|PN|BR|WG|BK|CB)(?![A-Za-z0-9])', re.I)


def models(text):
    text = str(text or '')
    # In UI firmware names, _G1/_G2/_G3 and (G3_HardWare) describe
    # generations, not the independent G-series products (e.g. G18/G20).
    text = re.sub(r'(UI\d{1,3}[A-Z]?)[_ -]+G[123](?![A-Za-z0-9])', r'\1 ', text, flags=re.I)
    text = re.sub(r'\(G[123][ _-]*(?:HardWare|硬件)\)', ' ', text, flags=re.I)
    text = re.sub(r'(UI\d{1,3}[A-Z]?)[_ -]+G[123](?=硬件|软件|国内|转)', r'\1 ', text, flags=re.I)
    text = re.sub(r'转G[123](?=软件|硬件|国内)', '转', text, flags=re.I)
    found = []
    for match in MODEL.finditer(text):
        if re.match(r'\s*(?:灯板|主板|芯片|电路板)', str(text)[match.end():]):
            continue
        value = match.group(1).upper()
        variant = VARIANT.match(str(text)[match.end():])
        if variant:
            value += ' ' + variant.group(1).upper()
        if value not in found:
            found.append(value)
    return found


def compatible(left, right):
    return left == right or left.startswith(right + ' ') or right.startswith(left + ' ')


def resolve_product(item, filename):
    item['notes'] = [note for note in item.get('notes', []) if not note.startswith(('型号存在冲突或多个候选：', '型号来源：', '正文未读取到明确型号，按文件名识别型号：'))]
    meta = item['metadata']
    names = models(Path(filename).stem)
    names = [v for v in names if not any(w != v and w.startswith(v + ' ') for w in names)]
    body = meta.get('product_body_candidates')
    current = str(item.get('product') or '').strip()
    if body is None:
        body = models(current)
    body = list(dict.fromkeys(body))
    # Repeated generic family and its single detailed variant are one identity.
    body = [v for v in body if not any(w != v and w.startswith(v + ' ') for w in body)]
    evidence = [{'source': '正文', 'product': v} for v in body] + [{'source': '文件名', 'product': v} for v in names]
    meta['product_candidates'] = evidence
    conflict = len(body) > 1 or len(names) > 1 or (body and names and not compatible(body[0], names[0]))
    if conflict:
        item['product'] = ''
        meta['product_reading_status'] = 'pending'
        meta['product_source'] = '需人工确认'
        item['notes'].append('型号存在冲突或多个候选：' + '；'.join(e['source'] + '：' + e['product'] for e in evidence) + '。请人工确认适用型号。')
    elif body:
        # Keep meaningful existing BOM product descriptions and colour names.
        item['product'] = current if models(current) and (models(current)[0] == body[0] or models(current)[0].startswith(body[0] + " ")) else body[0]
        meta['product_source'] = '正文'
        meta['product_reading_status'] = 'found'
        item['notes'].append('型号来源：正文' + ('，文件名一致。' if names else '。'))
    elif names:
        item['product'] = names[0]
        meta['product_source'] = '文件名'
        meta['product_reading_status'] = 'found'
        item['notes'].append('正文未读取到明确型号，按文件名识别型号：' + names[0] + '；未推断型号别名或适用范围。')
    return item
