"""Shared AI reply configuration helpers for every supported platform."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import requests

from app_paths import get_app_root
from ai_conversation_store import ai_conversation_store


AI_PLATFORM_KEYS = ('bili_message', 'bili_comment', 'xiaohongshu', 'weibo', 'douyin', 'xianyu', 'douyin_laike')
# 平台缺少独立配置时回退到的来源平台（抖音来客 → 抖音私信）
PLATFORM_FALLBACK = {'douyin_laike': 'douyin'}

LAIKE_DEFAULT_PERSONA = (
    '你是脸纪品牌的客服小妹，真人语气在抖音私信里跟客户聊天。'
    '说话自然口语化，像微信聊天一样，句子别太长，偶尔用个语气词（哈、呀、呢、哦）。'
    '别用太官方的套话，别每句都带“亲”，别把话说得像背书。'
    '先正常回答客户的问题，不要每条都追着要联系方式。只有客户明显感兴趣、问题问得差不多了，再自然地提一句。'
    '一次只说一两件事，别把所有卖点都堆在一段里。'
    '重要政策（必须牢记）：我们没有加盟费，拿货就可以合作。客户问加盟费/费用时，直接说没有加盟费、拿货就能合作，不要说“不方便说”“怕讲不准”。'
    '拿货的具体金额、数量、政策细节不要在私信里讲，转留资让老师介绍。'
)
LAIKE_OLD_FOLLOW_UP = (
    '你是脸纪品牌的客服小妹，真人语气在抖音私信里跟客户聊天。'
    '客户之前咨询过但已经一段时间没说话了，你发一条简短的跟进消息。'
    '要求：1）先自然地回一下之前聊的话题，别显得突兀；'
    '2）然后轻描淡写地引导留个联系方式（比如：方便的话留个联系方式，老师可以给你详细讲讲），只提一次，别硬推；不要主动提微信两个字；'
    '3）口语化、简短，一两句话就行，别用官方套话。'
    '注意：不要输出任何政策、优惠、价格数字；不要复述敏感词。'
)
LAIKE_PREV_FOLLOW_UP = (
    '客户在我们回复后大约两分钟没回消息，你接着刚才的对话补一句，像真人客服顺手跟一句。'
    '要求：1）只写一句话，简短口语化（不超过40个字）；'
    '2）紧扣客户最后问的那个问题往下接（例如客户问线下课程，就接着说课程/培训相关），不要换到别的话题；'
    '3）禁止使用“之前”“还有印象”“好久”“一段时间”“前面说的”这类像隔了很久的说法；'
    '4）不要重复我们上一条回复已经说过的内容；'
    '5）如果我们上一条已经请客户留联系方式，这次不要再原样要，换一个新的理由或好处（比如老师可以按他的情况单独讲、可以把相关资料直接发他），只轻轻提一句；'
    '6）不要主动提微信两个字，不要输出政策、优惠、价格数字，不要官方套话。'
)
LAIKE_DEFAULT_FOLLOW_UP = (
    '客户在我们回复后大约两分钟没回消息，你补发一句引导留电话的话。'
    '要求：只写一句陈述句（不超过35个字），可以顺着客户最后问的话题带一下，然后引导留电话，说老师会按他的情况联系他；'
    '不要重复我们上一条的原话，换个说法或给个新理由（比如资料可以直接发他、老师单独讲更清楚）；'
    '禁止任何提问：不能有问号，不能用“吗、呢、不、哪方面、方便吗、可以吗”这类问句；'
    '禁止“之前、还有印象、好久、一段时间”这类说法；不要主动提微信两个字，不要输出政策、优惠、价格数字。'
)
LAIKE_DEFAULT_REPLY_RULES = (
    '【回复硬性规则（优先于上面所有要求）】'
    '1）每条回复只写一两句：先用一句话简短回应客户问的内容（不展开细节），然后马上引导客户留电话，例如“具体的老师会按你的情况详细讲，留个电话我让老师联系你～”；客户只是打招呼（你好/在吗）也一样：简单问好后直接引导留电话；不要说“有什么想了解的可以跟我说”这类等客户继续聊的话；'
    '2）全程不准提问：不能出现问号，不能用“吗、呢、不、哪方面、方便吗、可以吗、是不是”等疑问说法，只用陈述句；'
    '3）不要跟客户多轮闲聊，不要反问客户的情况；'
    '4）如果客户已经留了电话号码，就不再引导留资，只回一句简短的感谢，例如“收到啦，老师稍后会联系你，请留意电话～”。'
)
LAIKE_FOLLOW_UP_FALLBACK = '具体情况老师会单独跟你讲，留个电话我安排老师联系你～'
LAIKE_NO_QUESTION_FALLBACK = LAIKE_FOLLOW_UP_FALLBACK
LAIKE_CONTACT_THANKS = ['收到，马上安排老师联系你～', '好的，安排老师跟你联系～', '收到啦，老师稍后联系你，留意下电话哈～',
                        '好的收到，马上让老师联系你～']
LAIKE_AD_NAME_KEYWORDS = ['官方号', '工作室', '旗舰店', '招商', '专营店', '企业号']
LAIKE_AD_TEXT_KEYWORDS = ['我是商家专属客服', '商家专属客服', '有需要随时联系', '有需要随时找我', '有任何需要随时', '评价消息卡片',
                          '诚邀您评价', '此消息由「', '祝您生活愉快', '祝你生活愉快', '欢迎光临本店', '感谢您的关注']
LAIKE_AFTER_LEAD_REPLIES = ['老师会跟你联系，给你具体说明～', '这块老师联系你的时候会详细跟你讲～', '好的，老师稍后联系你具体说明～']
_LAIKE_LIST_KEYS = {'blacklist': 60, 'contact_thanks': 200, 'after_lead_replies': 200, 'ad_name_keywords': 40, 'ad_text_keywords': 80}


def default_platform_settings(platform: str) -> Dict[str, Any]:
    if platform == 'douyin_laike':
        return {'persona': LAIKE_DEFAULT_PERSONA, 'follow_up_enabled': True,
                'follow_up_delay': 120, 'follow_up_prompt': LAIKE_DEFAULT_FOLLOW_UP,
                'skip_if_lead': True, 'lead_card_on_follow_up': True, 'blacklist': [],
                'reply_rules': LAIKE_DEFAULT_REPLY_RULES,
                'no_question_fallback': LAIKE_NO_QUESTION_FALLBACK, 'contact_thanks': list(LAIKE_CONTACT_THANKS),
                'after_lead_replies': list(LAIKE_AFTER_LEAD_REPLIES),
                'ad_name_keywords': list(LAIKE_AD_NAME_KEYWORDS), 'ad_text_keywords': list(LAIKE_AD_TEXT_KEYWORDS),
                'block_enterprise': True, 'loop_guard': True,
                'silent_greet_enabled': True, 'silent_greet_text': '在的，亲', 'silent_greet_delay': 0}
    return {}


def normalize_platform_settings(platform: str, value: Any) -> Dict[str, Any]:
    base = default_platform_settings(platform)
    value = value if isinstance(value, dict) else {}
    out = dict(base)
    for key in ('persona', 'follow_up_prompt', 'reply_rules', 'no_question_fallback', 'silent_greet_text'):
        if key in value:
            out[key] = str(value.get(key) or '')[:8000]
    if platform == 'douyin_laike' and (out.get('follow_up_prompt') or '').strip() in ('', LAIKE_OLD_FOLLOW_UP, LAIKE_PREV_FOLLOW_UP):
        out['follow_up_prompt'] = LAIKE_DEFAULT_FOLLOW_UP  # 旧默认模板（易产生“隔了很久”的说法）自动升级
    for key in ('follow_up_enabled', 'skip_if_lead', 'lead_card_on_follow_up', 'block_enterprise', 'loop_guard', 'silent_greet_enabled'):
        if key in value:
            out[key] = bool(value.get(key))
    if 'persona' in value and platform != 'douyin_laike':
        out['persona'] = str(value.get('persona') or '')[:8000]
    if isinstance(value.get('model_settings'), dict):
        out['model_settings'] = value['model_settings']  # 平台独立的上下文与安全设置（未设置则沿用全局）
    for lk, mx in _LAIKE_LIST_KEYS.items():
        if lk not in value:
            continue
        raw = value.get(lk)
        if isinstance(raw, str):
            raw = raw.splitlines()
        seen = []
        for n in raw if isinstance(raw, list) else []:
            n = str(n or '').strip()[:mx]
            if n and n not in seen:
                seen.append(n)
        out[lk] = seen[:500]
    if platform == 'douyin_laike':
        if not out.get('after_lead_replies'):
            out['after_lead_replies'] = list(LAIKE_AFTER_LEAD_REPLIES)
        if not out.get('contact_thanks'):
            out['contact_thanks'] = list(LAIKE_CONTACT_THANKS)
        if not (out.get('no_question_fallback') or '').strip():
            out['no_question_fallback'] = LAIKE_NO_QUESTION_FALLBACK
    if 'silent_greet_delay' in value:
        try:
            out['silent_greet_delay'] = max(0, min(600, int(float(value.get('silent_greet_delay')))))
        except (TypeError, ValueError):
            pass
    if platform == 'douyin_laike' and not (out.get('silent_greet_text') or '').strip():
        out['silent_greet_text'] = '在的，亲'
    if 'follow_up_delay' in value:
        try:
            out['follow_up_delay'] = max(30, min(86400, int(float(value.get('follow_up_delay')))))
        except (TypeError, ValueError):
            pass
    return out


def get_platform_settings(platform: str, provider: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    provider = provider or load_provider()
    all_settings = provider.get('platform_settings') if isinstance(provider.get('platform_settings'), dict) else {}
    return normalize_platform_settings(platform, all_settings.get(platform))
DEFAULT_MODEL_SETTINGS = {'context_enabled': True, 'context_window': 6000, 'auto_compress': True, 'prohibited_words': []}
DEFAULT_HANDOFF_SETTINGS = {'enabled': False}
PROHIBITED_MESSAGE_REPLY = '您发送的消息含有敏感或违禁词'

def effective_model_settings(provider: Dict[str, Any], platform: Optional[str] = None) -> Dict[str, Any]:
    """平台若保存了独立的上下文/安全设置则使用之，否则沿用全局 model_settings。"""
    if platform:
        ps = provider.get('platform_settings') if isinstance(provider.get('platform_settings'), dict) else {}
        own = (ps.get(platform) or {}).get('model_settings') if isinstance(ps.get(platform), dict) else None
        if isinstance(own, dict):
            return normalize_model_settings(own)
    return normalize_model_settings(provider.get('model_settings'))


def normalize_model_settings(value: Any) -> Dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_MODEL_SETTINGS)
    result['context_enabled'] = bool(value.get('context_enabled', True))
    try:
        result['context_window'] = max(500, min(128000, int(value.get('context_window', 6000))))
    except (TypeError, ValueError):
        result['context_window'] = 6000
    result['auto_compress'] = bool(value.get('auto_compress', True))
    def split_words(raw: Any) -> list[str]:
        values = [raw] if isinstance(raw, str) else list(raw or [])
        return [
            part.strip()
            for item in values
            for part in re.split(r'[,，]', str(item))
            if part.strip()
        ]

    raw = split_words(value.get('prohibited_words', []))
    # 旧版“敏感词”设置迁移到违禁词，避免升级后已有拦截规则静默丢失。
    legacy_sensitive = split_words(value.get('sensitive_words', []))
    combined = raw + legacy_sensitive
    result['prohibited_words'] = list(dict.fromkeys(
        str(item).strip() for item in combined if str(item).strip()
    ))[:500]
    return result


def normalize_platforms(value: Any) -> Dict[str, bool]:
    value = value if isinstance(value, dict) else {}
    out = {}
    for key in AI_PLATFORM_KEYS:
        if key in value:
            out[key] = bool(value.get(key))
        else:
            out[key] = bool(value.get(PLATFORM_FALLBACK.get(key, ''), False))
    return out


def normalize_handoff_settings(value: Any) -> Dict[str, bool]:
    value = value if isinstance(value, dict) else {}
    return {'enabled': bool(value.get('enabled', False))}


def load_app_config() -> Dict[str, Any]:
    try:
        with open(os.path.join(get_app_root(), 'config.json'), 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}


def load_provider() -> Dict[str, Any]:
    return load_app_config().get('ai_provider') or {}


def platform_enabled(platform: str, provider: Optional[Dict[str, Any]] = None) -> bool:
    provider = provider or load_provider()
    return bool(provider.get('enabled') and normalize_platforms(provider.get('platforms')).get(platform))


def normalize_knowledge_config(value: Any) -> Dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    bases = value.get('bases') if isinstance(value.get('bases'), list) else []
    assignments = value.get('platform_assignments') if isinstance(value.get('platform_assignments'), dict) else {}
    # Migrate the original single knowledge base in memory without losing user data.
    if not bases and (value.get('text') or value.get('documents')):
        bases = [{
            'id': 'legacy_default', 'name': '默认知识库',
            'text': str(value.get('text') or ''), 'documents': list(value.get('documents') or []),
        }]
        assignments = {key: ['legacy_default'] for key in AI_PLATFORM_KEYS}
    normalized_bases = []
    for base in bases:
        if not isinstance(base, dict) or not str(base.get('id') or ''):
            continue
        normalized_bases.append({
            'id': str(base.get('id')), 'name': str(base.get('name') or '未命名知识库'),
            'text': str(base.get('text') or ''), 'documents': list(base.get('documents') or []),
        })
    valid_ids = {base['id'] for base in normalized_bases}
    normalized_assignments = {
        key: [str(item) for item in (
            assignments.get(key) if key in assignments else assignments.get(PLATFORM_FALLBACK.get(key, ''))
        ) or [] if str(item) in valid_ids]
        for key in AI_PLATFORM_KEYS
    }
    return {'enabled': bool(value.get('enabled', False)), 'bases': normalized_bases, 'platform_assignments': normalized_assignments}


def build_knowledge_context(knowledge: Optional[Dict[str, Any]] = None, platform: Optional[str] = None) -> str:
    if knowledge is None:
        knowledge = load_app_config().get('ai_knowledge_base') or {}
    knowledge = normalize_knowledge_config(knowledge)
    if not knowledge['enabled']:
        return ''
    chunks = []
    selected_ids = set(knowledge['platform_assignments'].get(platform, [])) if platform else {base['id'] for base in knowledge['bases']}
    knowledge_root = os.path.abspath(os.path.join(get_app_root(), 'ai_knowledge'))
    for base in knowledge['bases']:
        if base['id'] not in selected_ids:
            continue
        base_chunks = []
        text = base['text'].strip()
        if text:
            base_chunks.append(text[:20000])
        for document in base['documents']:
            if document.get('content_in_text'):
                continue
            path = os.path.abspath(str(document.get('path') or ''))
            try:
                if os.path.commonpath([knowledge_root, path]) != knowledge_root:
                    continue
                with open(path, 'r', encoding='utf-8') as handle:
                    content = handle.read(20000).strip()
                if content:
                    base_chunks.append(f"文档：{document.get('name') or os.path.basename(path)}\n{content}")
            except (OSError, ValueError):
                continue
        if base_chunks:
            chunks.append(f"知识库：{base['name']}\n" + '\n\n'.join(base_chunks))
        if sum(len(chunk) for chunk in chunks) >= 60000:
            break
    if not chunks:
        return ''
    return '请优先依据以下知识库回答。知识库没有相关信息时，再使用通用知识。\n\n' + '\n\n---\n\n'.join(chunks)


def build_conversation_context(
    platform: str, contact_id: str, provider: Optional[Dict[str, Any]] = None,
) -> str:
    """Return recent, successful exchanges for one contact on one platform."""
    provider = provider or load_provider()
    settings = effective_model_settings(provider, platform)
    if not settings['context_enabled']:
        return ''
    # Leave room for the current message, instructions and assigned knowledge base.
    history_budget = max(250, min(60000, int(settings['context_window'] * 0.6)))
    return ai_conversation_store.build_context(platform, str(contact_id or ''), history_budget)


def record_conversation_exchange(
    platform: str, contact_id: str, user_message: str, assistant_message: str,
    event_key: str, provider: Optional[Dict[str, Any]] = None,
) -> bool:
    """Persist an exchange only while context is enabled and delivery succeeded."""
    provider = provider or load_provider()
    settings = effective_model_settings(provider, platform)
    if not settings['context_enabled']:
        return False
    return ai_conversation_store.record_exchange(
        platform, str(contact_id or ''), user_message, assistant_message, event_key,
    )


def _request_completion(provider: Dict[str, Any], prompt: str, max_tokens: int = 256) -> Optional[str]:
    fmt = provider.get('format', 'openai')
    base_url = str(provider.get('base_url', '')).rstrip('/')
    model = provider.get('model', '')
    try:
        if fmt == 'anthropic':
            url = base_url if base_url.endswith('/messages') else base_url + '/messages'
            response = requests.post(
                url,
                headers={'x-api-key': provider['api_key'], 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
                json={'model': model, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': prompt}]},
                timeout=20,
            )
            body = response.json()
            return (body.get('content') or [{}])[0].get('text') if response.ok else None
        url = base_url if base_url.endswith('/chat/completions') else base_url + '/chat/completions'
        payload = {'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': max_tokens}
        # deepseek-v4-pro 默认开启推理模式，直接关掉，避免返回空 content
        if 'deepseek' in model:
            payload['thinking'] = {'type': 'disabled'}
        response = requests.post(
            url,
            headers={'Authorization': f"Bearer {provider['api_key']}", 'Content-Type': 'application/json'},
            json=payload,
            timeout=20,
        )
        body = response.json()
        return ((body.get('choices') or [{}])[0].get('message') or {}).get('content') if response.ok else None
    except (requests.RequestException, ValueError, IndexError, TypeError):
        return None


def _prepare_prompt(
    message: str, context: str, provider: Dict[str, Any],
    knowledge: Optional[Dict[str, Any]], platform: Optional[str],
) -> tuple[str, Dict[str, Any]]:
    settings = effective_model_settings(provider, platform)
    knowledge_context = build_knowledge_context(knowledge, platform) if settings['context_enabled'] else ''
    # Put recent conversation immediately before the current message. When automatic
    # compression keeps the tail, user history survives ahead of less relevant knowledge.
    prompt = '\n\n'.join(part for part in (knowledge_context, context, f'用户消息：{message}') if part).strip()
    if not settings['context_enabled']:
        prompt = f'用户消息：{message}'
    if len(prompt) > settings['context_window']:
        prompt = prompt[-settings['context_window']:] if settings['auto_compress'] else prompt[:settings['context_window']]
    restrictions = settings['prohibited_words']
    if restrictions:
        prompt += '\n\n请勿输出或复述以下词语：' + '、'.join(restrictions)
    if platform and platform != 'douyin_laike':
        persona = get_platform_settings(platform, provider).get('persona', '').strip()
        if persona:
            prompt = f'【客服人设与回复要求】\n{persona}\n\n' + prompt
    return prompt, settings


def prohibited_message_reply(message: str, provider: Optional[Dict[str, Any]] = None, platform: Optional[str] = None) -> Optional[str]:
    """Return the fixed reply when an incoming message contains a configured prohibited word."""
    provider = provider or load_provider()
    message_folded = str(message or '').casefold()
    if not message_folded:
        return None
    settings = effective_model_settings(provider, platform)
    for word in settings['prohibited_words']:
        if word.casefold() in message_folded:
            return PROHIBITED_MESSAGE_REPLY
    return None


def reply_contains_prohibited(reply: str, provider: Optional[Dict[str, Any]] = None, platform: Optional[str] = None) -> Optional[str]:
    """检查AI生成的回复是否含极限词，返回命中的词，没有则返回None。"""
    provider = provider or load_provider()
    reply_folded = str(reply or '').casefold()
    if not reply_folded:
        return None
    settings = effective_model_settings(provider, platform)
    for word in settings['prohibited_words']:
        w = str(word or '').strip()
        if w and w.casefold() in reply_folded:
            return w
    return None


def generate_reply(message: str, context: str = '', provider: Optional[Dict[str, Any]] = None,
                   knowledge: Optional[Dict[str, Any]] = None, platform: Optional[str] = None) -> Optional[str]:
    provider = provider or load_provider()
    # 不再因客户消息含违禁词而拒回：AI 正常回复，输出时自动避开违禁词
    if not provider.get('enabled') or not provider.get('api_key'):
        return None
    prompt, _ = _prepare_prompt(message, context, provider, knowledge, platform)
    return _request_completion(provider, prompt, max_tokens=256)


def get_safe_fallback_reply(user_message: str = '') -> str:
    """按客户问题类型返回对应的安全兜底回复，均不含违禁词。"""
    msg = str(user_message or '')
    # 问线下/课程/会议类：不复述关键词，只说不清楚+转老师
    if any(k in msg for k in ('线下', '课程', '会议', '面谈', '听课', '讲解')):
        return (
            '这个我不太清楚，具体情况可以咨询我们的老师。'
            '你可以留个联系方式，让老师跟你详细说下。'
        )
    # 问赚钱/加盟/费用类：正面回应不承诺+转留资
    if any(k in msg for k in ('赚钱', '加盟', '费用', '收费', '报价', '多少钱', '政策', '优惠')):
        return (
            '你这个担心我特别理解，做生意确实得谨慎些。'
            '脸纪这边不用加盟费，直接拿货就能做，前期投入小一些。'
            '具体适不适合你、怎么做更稳，可以留个联系方式，让老师帮你分析下。'
        )
    # 通用兜底
    return (
        '你这个问题我这边一下说不太清楚，留个联系方式，'
        '让老师跟你详细聊聊。'
    )


# 兼容旧引用
SAFE_FALLBACK_REPLY = get_safe_fallback_reply('加盟')


def generate_reply_decision(
    message: str, context: str = '', provider: Optional[Dict[str, Any]] = None,
    knowledge: Optional[Dict[str, Any]] = None, platform: Optional[str] = None,
    avoid_words: Optional[list] = None,
) -> Dict[str, Any]:
    """Generate a reply or explicitly route the message to a human queue."""
    provider = provider or load_provider()
    # 不再因客户消息含违禁词而拒回：AI 正常回复，输出时自动避开违禁词
    handoff = normalize_handoff_settings(provider.get('human_handoff'))
    if not handoff['enabled']:
        return {'needs_human': False, 'reason': '', 'reply': generate_reply(message, context, provider, knowledge, platform)}
    if not provider.get('enabled') or not provider.get('api_key'):
        return {'needs_human': True, 'reason': 'AI 服务当前不可用', 'reply': None}

    base_prompt, _ = _prepare_prompt(message, context, provider, knowledge, platform)
    if avoid_words:
        base_prompt += '\n\n重要：上一版回复违规含极限词，本次绝对不许出现以下任何词：' + '、'.join(avoid_words)
    instruction = (
        '你同时负责客服回复和人工升级判断。仅在以下情况标记为需要人工：'
        '缺少可靠信息且无法安全回答；知识库无法支持具体业务结论；涉及退款、支付、订单、账号权限、'
        '投诉争议、法律或隐私；用户明确要求人工；继续自动回答可能误导用户。'
        '普通问候、常规咨询和能够依据知识库回答的问题不要升级。'
        '只输出一个 JSON 对象，不要使用 Markdown：'
        '{"needs_human":false,"reason":"","reply":"给用户的回复"}。'
        '需要人工时 reply 必须为空，并用一句简短中文说明原因。'
    )
    raw = _request_completion(provider, instruction + '\n\n' + base_prompt, max_tokens=384)
    if not raw:
        return {'needs_human': True, 'reason': 'AI 未能生成可靠回复', 'reply': None}
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', str(raw).strip(), flags=re.I)
    match = re.search(r'\{.*\}', cleaned, flags=re.S)
    try:
        parsed = json.loads(match.group(0) if match else cleaned)
    except (ValueError, TypeError, AttributeError):
        # Compatible fallback for models that ignore the JSON-only instruction.
        return {'needs_human': False, 'reason': '', 'reply': cleaned.strip() or None}
    needs_human = parsed.get('needs_human') is True or str(parsed.get('needs_human', '')).lower() == 'true'
    reason = str(parsed.get('reason') or '').strip()[:240]
    reply = str(parsed.get('reply') or '').strip()
    if needs_human or not reply:
        return {'needs_human': True, 'reason': reason or 'AI 无法确认可靠回复', 'reply': None}
    return {'needs_human': False, 'reason': '', 'reply': reply}
