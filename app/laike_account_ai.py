"""每个抖音来客账号自己的 AI 开关、话术和知识库。

不读「平台共用」的 douyin_laike 设置作为运行时回退。升级时把当时的共用配置
复制进默认账号（以及其他还没有这份文件的旧账号），之后各自修改互不影响。
新添加的账号写入 source=empty，除非明确 copy_from。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

AI_FILE = 'laike_account_ai.json'
PLATFORM = 'douyin_laike'


def empty_document() -> Dict[str, Any]:
    return {
        'version': 1,
        'ai_enabled': False,
        'settings': {},
        'knowledge': _empty_knowledge(),
        'source': 'empty',
        'migrated_from_shared': False,
    }


def _empty_knowledge() -> Dict[str, Any]:
    return {'enabled': False, 'bases': [], 'platform_assignments': {PLATFORM: []}}


def load_document(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or 'ai_enabled' not in data:
        return None
    return data


def save_document(path: str, doc: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def user_root_for(data_dir: str, legacy: bool) -> str:
    """来客账号数据目录对应的用户根目录（config.json 所在处）。"""
    if legacy:
        return data_dir
    # <root>/douyin_laike_accounts/<id>
    return os.path.dirname(os.path.dirname(data_dir))


def read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def normalize_account_knowledge(value: Any) -> Dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    bases: List[Dict[str, Any]] = []
    for base in value.get('bases') or []:
        if not isinstance(base, dict) or not str(base.get('id') or '').strip():
            continue
        documents = []
        for doc in base.get('documents') or []:
            if not isinstance(doc, dict):
                continue
            documents.append({
                'id': str(doc.get('id') or ''),
                'name': str(doc.get('name') or ''),
                'size': int(doc.get('size') or 0),
            })
        bases.append({
            'id': str(base.get('id')),
            'name': str(base.get('name') or '未命名知识库')[:100],
            'text': str(base.get('text') or ''),
            'documents': documents,
        })
    valid = {base['id'] for base in bases}
    raw = value.get('platform_assignments') if isinstance(value.get('platform_assignments'), dict) else {}
    chosen = raw.get(PLATFORM)
    if chosen is None:
        assigned = [base['id'] for base in bases]
    else:
        assigned = []
        for item in chosen or []:
            item = str(item)
            if item in valid and item not in assigned:
                assigned.append(item)
    return {
        'enabled': bool(value.get('enabled', False)),
        'bases': bases,
        'platform_assignments': {PLATFORM: assigned},
    }


def _inline_base_text(base: Dict[str, Any], knowledge_root: str) -> str:
    parts: List[str] = []
    text = str(base.get('text') or '').strip()
    if text:
        parts.append(text)
    root = os.path.abspath(knowledge_root) if knowledge_root else ''
    for doc in base.get('documents') or []:
        if not isinstance(doc, dict) or doc.get('content_in_text'):
            continue
        path = os.path.abspath(str(doc.get('path') or ''))
        if not path or not root:
            continue
        try:
            if os.path.commonpath([root, path]) != root:
                continue
            with open(path, 'r', encoding='utf-8') as handle:
                content = handle.read(200000).strip()
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if content and content not in text:
            parts.append(content)
    return '\n\n'.join(parts)[:200000]


def copy_assigned_knowledge(raw: Any, knowledge_root: str) -> Dict[str, Any]:
    """把当时分给抖音来客的知识库复制成这个账号自己的副本（新 id，不改共用文件）。"""
    from ai_reply_service import normalize_knowledge_config
    shared = normalize_knowledge_config(raw or {})
    assigned = list(shared['platform_assignments'].get(PLATFORM) or [])
    copied: List[Dict[str, Any]] = []
    id_map: Dict[str, str] = {}
    for base in shared['bases']:
        if base['id'] not in assigned:
            continue
        new_id = 'lk' + os.urandom(6).hex()
        id_map[base['id']] = new_id
        copied.append({
            'id': new_id,
            'name': base['name'],
            'text': _inline_base_text(base, knowledge_root),
            'documents': [
                {'id': str(doc.get('id') or ''), 'name': str(doc.get('name') or ''), 'size': int(doc.get('size') or 0)}
                for doc in (base.get('documents') or []) if isinstance(doc, dict)
            ],
        })
    return normalize_account_knowledge({
        'enabled': bool(shared.get('enabled')) and bool(copied),
        'bases': copied,
        'platform_assignments': {PLATFORM: [id_map[item] for item in assigned if item in id_map]},
    })


def snapshot_from_shared(config: Dict[str, Any], override: Optional[Dict[str, Any]] = None,
                         knowledge_root: str = '', source: str = 'migrated') -> Dict[str, Any]:
    """把 config.json 里当时的抖音来客设置和知识库复制出来。共用文件本身不修改。"""
    provider = config.get('ai_provider') if isinstance(config.get('ai_provider'), dict) else {}
    platforms = provider.get('platforms') if isinstance(provider.get('platforms'), dict) else {}
    if PLATFORM in platforms:
        ai_enabled = bool(platforms.get(PLATFORM))
    else:
        ai_enabled = bool(platforms.get('douyin', False))
    all_settings = provider.get('platform_settings') if isinstance(provider.get('platform_settings'), dict) else {}
    settings = dict(all_settings.get(PLATFORM) or {}) if isinstance(all_settings.get(PLATFORM), dict) else {}
    if override and override.get('enabled') and isinstance(override.get('settings'), dict):
        settings.update(override['settings'])
        if 'ai_enabled' in override['settings']:
            ai_enabled = bool(override['settings'].get('ai_enabled'))
        source = source + '+override'
    return {
        'version': 1,
        'ai_enabled': ai_enabled,
        'settings': settings,
        'knowledge': copy_assigned_knowledge(config.get('ai_knowledge_base') or {}, knowledge_root),
        'source': source,
        'migrated_from_shared': source.startswith('migrated'),
    }


def clone_document(src: Dict[str, Any]) -> Dict[str, Any]:
    """复制另一账号的 AI / 话术 / 知识库。知识库换成新 id，避免两份互相改到。"""
    knowledge = normalize_account_knowledge(src.get('knowledge'))
    id_map: Dict[str, str] = {}
    bases = []
    for base in knowledge['bases']:
        new_id = 'lk' + os.urandom(6).hex()
        id_map[base['id']] = new_id
        copied = dict(base)
        copied['id'] = new_id
        bases.append(copied)
    knowledge['bases'] = bases
    knowledge['platform_assignments'] = {
        PLATFORM: [id_map[item] for item in knowledge['platform_assignments'].get(PLATFORM, []) if item in id_map],
    }
    settings = src.get('settings') if isinstance(src.get('settings'), dict) else {}
    return {
        'version': 1,
        'ai_enabled': bool(src.get('ai_enabled')),
        'settings': json.loads(json.dumps(settings, ensure_ascii=False)),
        'knowledge': knowledge,
        'source': 'copy',
        'migrated_from_shared': False,
    }


def public_settings(doc: Dict[str, Any]) -> Dict[str, Any]:
    from ai_reply_service import normalize_platform_settings
    return normalize_platform_settings(PLATFORM, doc.get('settings') or {})


def merge_settings(doc: Dict[str, Any], patch: Dict[str, Any], use_global_model_settings: bool = False) -> Dict[str, Any]:
    from ai_reply_service import normalize_model_settings, normalize_platform_settings
    current = dict(doc.get('settings') or {})
    incoming = dict(patch or {})
    incoming.pop('use_global_model_settings', None)
    if use_global_model_settings:
        current.pop('model_settings', None)
        incoming.pop('model_settings', None)
    elif isinstance(incoming.get('model_settings'), dict):
        incoming['model_settings'] = normalize_model_settings(incoming['model_settings'])
    current.update(incoming)
    normalized = normalize_platform_settings(PLATFORM, current)
    if not isinstance(current.get('model_settings'), dict):
        normalized.pop('model_settings', None)
    doc = dict(doc)
    doc['settings'] = normalized
    return doc


def save_knowledge_base(doc: Dict[str, Any], base_id: str, name: str, text: str):
    knowledge = normalize_account_knowledge(doc.get('knowledge'))
    name = (name or '').strip()
    if not name:
        raise ValueError('请填写知识库名称')
    if len(text or '') > 2 * 1024 * 1024:
        raise ValueError('知识库内容不能超过 2MB')
    found = None
    for base in knowledge['bases']:
        if base['id'] == base_id:
            base['name'] = name[:100]
            base['text'] = text or ''
            found = base
            break
    if found is None:
        found = {'id': 'lk' + os.urandom(6).hex(), 'name': name[:100], 'text': text or '', 'documents': []}
        knowledge['bases'].append(found)
        knowledge['platform_assignments'][PLATFORM].append(found['id'])
    doc = dict(doc)
    doc['knowledge'] = knowledge
    return doc, found


def save_knowledge_settings(doc: Dict[str, Any], enabled: bool, assigned_ids: List[str]) -> Dict[str, Any]:
    knowledge = normalize_account_knowledge(doc.get('knowledge'))
    valid = {base['id'] for base in knowledge['bases']}
    chosen = []
    for item in assigned_ids or []:
        item = str(item)
        if item in valid and item not in chosen:
            chosen.append(item)
    knowledge['enabled'] = bool(enabled)
    knowledge['platform_assignments'] = {PLATFORM: chosen}
    doc = dict(doc)
    doc['knowledge'] = knowledge
    return doc


def knowledge_for_prompt(doc: Dict[str, Any]) -> Dict[str, Any]:
    """交给 build_knowledge_context 的结构。内容已经内联在 text 里。"""
    knowledge = normalize_account_knowledge(doc.get('knowledge'))
    return {
        'enabled': knowledge['enabled'],
        'bases': knowledge['bases'],
        'platform_assignments': {PLATFORM: list(knowledge['platform_assignments'].get(PLATFORM) or [])},
    }
