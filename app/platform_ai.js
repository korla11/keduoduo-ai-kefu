/* 平台页内的 AI 设置菜单：AI 接口（所有平台共用）、AI 回复设置 / 知识库 / 人工待回（按平台）。
 * 必须在 platform_tabs.js 之前加载：本脚本同步插入 section，由 platform_tabs.js 生成左侧菜单。 */
(() => {
    'use strict';
    const body = document.body;
    const P = body.dataset.aiPlatform || body.dataset.apiPrefix || '';
    const NAMES = {douyin_laike: '抖音来客', douyin: '抖音私信', xiaohongshu: '小红书私信'};
    if (!NAMES[P]) return;
    const content = document.querySelector('.container > .main-content');
    if (!content) return;
    const NAME = NAMES[P];
    const MASK = '••••••••••••';
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    const $ = sel => document.querySelector(sel);

    const style = document.createElement('style');
    style.textContent = `
.pai .pai-note{display:flex;gap:8px;align-items:center;padding:9px 12px;border-radius:10px;background:var(--workspace-accent-soft,#effaf8);border:1px solid var(--workspace-accent-border,#bfe8e2);color:var(--workspace-accent-deep,#0b7f72);font-size:13px;margin:0 0 14px}
.pai .pai-note.gold{background:#fff8e8;border-color:#f1dfb5;color:#8a6420}
.pai .pai-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 16px}
.pai .pai-grid .wide{grid-column:1/-1}
.pai label.f{display:flex;flex-direction:column;gap:6px;font-size:13px;color:#5b6876}
.pai input[type=text],.pai input[type=url],.pai input[type=password],.pai input[type=number],.pai select,.pai textarea{width:100%;box-sizing:border-box;padding:9px 11px;border:1px solid #e2e7ed;border-radius:10px;font-size:13px;background:#fbfcfd;font-family:inherit}
.pai textarea{resize:vertical;line-height:1.6}
.pai input:focus,.pai select:focus,.pai textarea:focus{outline:none;border-color:var(--workspace-accent,#12a594);background:#fff}
.pai .pai-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:14px}
.pai .pai-res{font-size:13px;color:#7a8796}.pai .pai-res.ok{color:#389e0d}.pai .pai-res.err{color:#cf1322}
.pai .pai-card{border:1px solid #e9edf2;border-radius:12px;padding:14px;margin-top:14px;background:#fff}
.pai .pai-card h4{margin:0 0 4px;font-size:14px}.pai .pai-card p.sub{margin:0 0 10px;color:#8a96a3;font-size:12px}
.pai .pai-switch{display:flex;align-items:center;gap:10px;font-weight:600;cursor:pointer}
.pai .pai-switch input{width:40px;height:22px;appearance:none;background:#d5dbe2;border-radius:999px;position:relative;cursor:pointer;transition:.2s;margin:0}
.pai .pai-switch input:before{content:"";position:absolute;width:18px;height:18px;border-radius:50%;background:#fff;top:2px;left:2px;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
.pai .pai-switch input:checked{background:var(--workspace-accent,#12a594)}.pai .pai-switch input:checked:before{left:20px}
.pai .pai-kb{display:flex;flex-direction:column;gap:8px}
.pai .pai-kb label{display:flex;align-items:center;gap:8px;padding:9px 12px;border:1px solid #e9edf2;border-radius:10px;cursor:pointer;font-size:13px}
.pai .pai-kb label small{margin-left:auto;color:#9aa5b1}.pai .pai-kb label button{margin-left:6px}
.pai .pai-ho{border:1px solid #e9edf2;border-radius:10px;padding:10px 12px;margin-top:8px;font-size:13px}
.pai .pai-ho .meta{color:#8a96a3;font-size:12px;margin-bottom:4px}
.pai .pai-ms[data-disabled="1"]{opacity:.55;pointer-events:none}
@media(max-width:720px){.pai .pai-grid{grid-template-columns:1fr}}`;
    document.head.append(style);

    function section(cls, title, html) {
        const s = document.createElement('section');
        s.className = `${cls} pai`;
        s.innerHTML = `<h2>${title}</h2>${html}`;
        return s;
    }
    const isLaike = P === 'douyin_laike';
    const api = section('ai-api-panel', 'AI 接口', `
<div class="pai-note">🔗 所有平台共用：这里的接口地址、模型和 API Key 只有一份，在任一平台页面修改都会同步到全部平台。</div>
<label class="pai-switch" style="margin-bottom:14px"><input type="checkbox" id="pai-enabled"><span>启用 AI 回复（总开关，所有平台共用）</span></label>
<div class="pai-grid">
<label class="f">协议格式<select id="pai-format"><option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic 兼容</option><option value="custom">自定义兼容</option></select></label>
<label class="f">模型名称<input id="pai-model" type="text" list="pai-models" placeholder="选择模型或手动填写"><datalist id="pai-models"></datalist></label>
<label class="f wide">接口地址 (Base URL)<input id="pai-base" type="url" placeholder="https://api.deepseek.com"></label>
<label class="f wide">API Key<input id="pai-key" type="password" autocomplete="new-password" placeholder="请输入 API Key"></label>
</div>
<div class="pai-row"><button class="btn-primary" type="button" id="pai-api-save">保存接口设置</button><button class="btn-secondary" type="button" id="pai-test">测试连接</button><button class="btn-secondary" type="button" id="pai-models-btn">获取模型列表</button><button class="btn-secondary" type="button" id="pai-latency">延迟测速</button><span class="pai-res" id="pai-api-res"></span></div>
<p class="help-text" style="margin-top:10px">已保存的 API Key 以掩码显示；不修改掩码时继续使用原密钥。</p>`);

    const reply = section('ai-reply-panel', isLaike ? 'AI 回复 / 话术' : 'AI 回复设置', `
<div class="pai-note gold" id="pai-reply-note">${isLaike ? '每个来客账号单独一套人设、话术和策略。当前修改只写入选中的那个账号。' : `⚙️ 仅对「${NAME}」生效，其他平台各自独立设置。`}</div>
<label class="pai-switch"><input type="checkbox" id="pai-plat-on"><span>${isLaike ? '在当前来客账号启用 AI 回复' : `在 ${NAME} 启用 AI 回复`}</span></label>
<p class="help-text" id="pai-plat-hint" style="margin:6px 0 0"></p>
<div class="pai-card"><h4>客服人设 / 回复提示词</h4><p class="sub">${isLaike ? 'AI 回复抖音来客客户时使用的系统人设与回复规则' : '可选：填写后会作为本平台 AI 回复的人设要求加入提示词；留空则保持原有回复逻辑'}</p>
<textarea id="pai-persona" rows="8"></textarea>${isLaike ? `<h4 style="margin-top:12px">回复硬性规则（留资导向，不提问）</h4><p class="sub">追加在人设之后，普通回复和二次触达都会遵守；含问号的回复会自动重写为陈述句</p><textarea id="pai-rules" rows="6"></textarea>
<h4 style="margin-top:12px">提问兜底陈述句</h4><p class="sub">AI 回复/二次触达重新生成后仍含提问、或二次触达生成失败时，改发这句</p><input id="pai-noq" type="text">` : ''}</div>
${isLaike ? `<div class="pai-card"><h4>二次触达</h4><p class="sub">客户在 AI 回复后一段时间未回复时，自动发送一条跟进消息（每个客户只发一次）</p>
<div class="pai-row" style="margin-top:0"><label><input type="checkbox" id="pai-fu-on"> 启用二次触达</label><label>等待 <input type="number" id="pai-fu-delay" min="30" max="86400" style="width:100px;display:inline-block"> 秒</label><label><input type="checkbox" id="pai-fu-skip"> 客户已留资则不触达</label><label><input type="checkbox" id="pai-fu-card"> 二次触达时发送一键留资</label></div>
<textarea id="pai-fu-prompt" rows="5" style="margin-top:10px"></textarea></div>
<div class="pai-card"><h4>进线未发言主动回复</h4><p class="sub">新进线客户只有平台自动消息（欢迎语/评价卡片等）、自己没发言时，主动发一次这句话（每个会话仅一次）。不触发二次触达和一键留资；客户之后发言再走正常回复流程。跳过黑名单、已留资、已屏蔽及我方已发过消息的会话。</p>
<div class="pai-row" style="margin-top:0"><label><input type="checkbox" id="pai-sg-on"> 启用</label><label>检测到后等待 <input type="number" id="pai-sg-delay" min="0" max="600" style="width:80px;display:inline-block"> 秒发送</label></div>
<input id="pai-sg-text" type="text" style="margin-top:10px"></div>
<div class="pai-card"><h4>客户已留资后的确认回复</h4><p class="sub">客户发来电话/微信后，从下面随机选一句回复（每行一句）；之后不再引导留资、不发二次触达和一键留资。</p>
<textarea id="pai-thanks" rows="4"></textarea>
<h4 style="margin-top:12px">已留资后继续咨询的回复</h4><p class="sub">客户留资后再发消息时，从下面随机选一句回复（每行一句）；不调用 AI、不提问、不引导留资、不二次触达。</p>
<textarea id="pai-afterlead" rows="3"></textarea>
<p class="sub" style="margin-top:8px">留资识别（固定规则）：11 位手机号（1[3-9] 开头，允许空格/横线，不误判订单号）；微信号（“微信/vx/wx/加我”+ 号码，或单独发送的 6–20 位字母开头含数字的号）；来客「留资/一键留资」卡片提交或系统留资消息；已有留资记录。</p></div>
<div class="pai-card"><h4>黑名单（不自动回复）</h4><p class="sub">每行一个昵称（完全匹配）。名单内会话不自动回复、不二次触达、不发一键留资，新消息进入人工待回。广告/商家号与机器人循环会自动识别并转人工。</p>
<textarea id="pai-bl" rows="4" placeholder="例如：SH美学工作室官方号"></textarea></div>
<div class="pai-card"><h4>商家机器人话术识别</h4><p class="sub">按单条消息判断：只有命中「机器人话术关键词」的那条消息不自动回复（转人工待回）；商家的正常咨询照常回复并引导留资。昵称关键词、企业号/蓝V 只作为嫌疑信号；嫌疑账号或话术在我方回复后 15 秒内连续 3 轮秒回，才停止该会话自动回复。</p>
<div class="pai-row" style="margin-top:0"><label><input type="checkbox" id="pai-ent"> 企业号/蓝V 作为嫌疑信号</label><label><input type="checkbox" id="pai-loop"> 机器人循环防护（连续 3 轮秒回 → 停止并转人工待回）</label></div>
<div class="pai-grid" style="margin-top:10px"><label class="f">嫌疑昵称关键词（每行一个，仅作信号）<textarea id="pai-adn" rows="6"></textarea></label>
<label class="f">机器人话术关键词（每行一个，命中的消息不回复）<textarea id="pai-adt" rows="6"></textarea></label></div></div>` : ''}
<div class="pai-card"><h4>上下文与安全</h4><p class="sub">默认沿用全局设置；选择「本平台独立设置」后仅对 ${NAME} 生效</p>
<div class="pai-row" style="margin-top:0"><label><input type="radio" name="pai-ms-mode" value="global"> 沿用全局设置</label><label><input type="radio" name="pai-ms-mode" value="own"> 本平台独立设置</label></div>
<div class="pai-grid pai-ms" style="margin-top:12px">
<label class="f">上下文<select id="pai-ctx"><option value="true">开启</option><option value="false">关闭</option></select></label>
<label class="f">上下文窗口大小<input id="pai-win" type="number" min="500" max="128000" step="100"></label>
<label class="f">自动压缩<select id="pai-comp"><option value="true">开启</option><option value="false">关闭</option></select></label>
<label class="f wide">违禁词（中文逗号分隔，AI 回复时会避开）<textarea id="pai-words" rows="3"></textarea></label>
</div></div>
<div class="pai-row"><button class="btn-primary" type="button" id="pai-reply-save">保存 ${NAME} AI 设置</button><span class="pai-res" id="pai-reply-res"></span></div>`);

    const kb = section('ai-kb-panel', '知识库', `
<div class="pai-note gold" id="pai-kb-note">${isLaike ? '知识库只属于当前选中的来客账号，不会和其他来客账号、抖音私信或小红书共用。' : `📚 知识库内容所有平台共用；下面勾选「${NAME}」要使用哪些知识库。`}</div>
<label class="pai-switch"><input type="checkbox" id="pai-kb-on"><span>${isLaike ? '启用当前账号的知识库' : '启用知识库（所有平台共用）'}</span></label>
<div class="pai-card"><h4>${NAME} 使用的知识库</h4><p class="sub">可选择一个或多个</p><div class="pai-kb" id="pai-kb-list">加载中…</div>
<div class="pai-row"><button class="btn-primary" type="button" id="pai-kb-save">保存知识库选择</button><span class="pai-res" id="pai-kb-res"></span></div></div>
<div class="pai-card"><h4>编辑知识库内容</h4><p class="sub">${isLaike ? '只影响当前这个来客账号' : '修改内容会影响所有使用该知识库的平台'}</p>
<div class="pai-grid"><label class="f">选择<select id="pai-kb-sel"></select></label><label class="f">名称<input id="pai-kb-name" type="text" maxlength="100"></label>
<label class="f wide">内容<textarea id="pai-kb-text" rows="10"></textarea></label></div>
<div class="pai-row"><button class="btn-primary" type="button" id="pai-kb-text-save">保存知识库内容</button><span class="pai-res" id="pai-kb-text-res"></span></div></div>`);

    const ho = section('ai-handoff-panel', '人工待回', `
<div class="pai-note gold">🙋 AI 无法可靠处理时暂停自动回复并加入人工待回列表（开关所有平台共用，${isLaike ? '列表只显示当前来客账号' : `列表仅显示 ${NAME}`}）。</div>
<label class="pai-switch"><input type="checkbox" id="pai-ho-on"><span>启用人工待回</span></label>
<div class="pai-row"><button class="btn-secondary" type="button" id="pai-ho-refresh">刷新列表</button><span class="pai-res" id="pai-ho-res"></span></div>
<div id="pai-ho-list" style="margin-top:8px"></div>`);

    // 插入到「运行日志」之前
    const logPanel = content.querySelector(':scope > section.log-panel');
    [api, reply, kb, ho].forEach(s => content.insertBefore(s, logPanel || null));

    const res = (id, text, kind) => { const el = document.getElementById(id); el.textContent = text; el.className = 'pai-res' + (kind ? ' ' + kind : ''); };
    function withAccount(url) {
        if (!isLaike || url.indexOf('/api/laike-accounts/') !== 0) return url;
        const id = window.laikeCurrentAccountId || '';
        if (!id || /[?&]account_id=/.test(url)) return url;
        return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'account_id=' + encodeURIComponent(id);
    }
    async function j(url, opts) {
        url = withAccount(url);
        const r = await fetch(url, opts && opts.body ? {method: opts.method || 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(opts.body)} : opts);
        let d = {}; try { d = await r.json(); } catch (_) {}
        if (!r.ok || d.success === false) throw new Error(d.error || `请求失败 (${r.status})`);
        return d;
    }
    let keyConfigured = false;
    function apiPayload() {
        const key = $('#pai-key').value;
        return {format: $('#pai-format').value, base_url: $('#pai-base').value.trim(), model: $('#pai-model').value.trim(), api_key: key === MASK ? '' : key.trim()};
    }
    async function loadApi() {
        const c = await j('/api/ai-config');
        $('#pai-enabled').checked = !!c.enabled;
        $('#pai-format').value = c.format || 'openai';
        $('#pai-base').value = c.base_url || '';
        $('#pai-model').value = c.model || '';
        keyConfigured = !!c.api_key_configured;
        $('#pai-key').value = keyConfigured ? MASK : '';
        $('#pai-plat-on').checked = !!(c.platforms || {})[P];
        $('#pai-ho-on').checked = !!(c.human_handoff || {}).enabled;
        $('#pai-plat-hint').textContent = c.enabled ? '' : '提示：AI 总开关当前关闭（在「AI 接口」中开启），平台开关保存后待总开关开启才生效。';
        return c;
    }
    $('#pai-key').addEventListener('focus', e => { if (e.target.value === MASK) e.target.value = ''; });
    $('#pai-key').addEventListener('blur', e => { if (!e.target.value && keyConfigured) e.target.value = MASK; });
    $('#pai-api-save').onclick = async () => {
        const p = apiPayload(); p.enabled = $('#pai-enabled').checked;
        if (p.enabled && (!p.base_url || !p.model || (!p.api_key && !keyConfigured))) return res('pai-api-res', '启用 AI 前请完整填写接口地址、模型和 API Key', 'err');
        res('pai-api-res', '正在保存…');
        try { await j('/api/ai-config', {body: p}); await loadApi(); res('pai-api-res', '已保存（所有平台同步生效）', 'ok'); } catch (e) { res('pai-api-res', e.message, 'err'); }
    };
    $('#pai-test').onclick = async () => { res('pai-api-res', '正在测试连接…'); try { const d = await j('/api/ai-test', {body: apiPayload()}); res('pai-api-res', d.message || '连接成功', 'ok'); } catch (e) { res('pai-api-res', e.message, 'err'); } };
    $('#pai-models-btn').onclick = async () => { res('pai-api-res', '正在获取模型…'); try { const d = await j('/api/ai-models', {body: apiPayload()}); $('#pai-models').innerHTML = (d.models || []).map(m => `<option value="${esc(m)}">`).join(''); res('pai-api-res', `已获取 ${(d.models || []).length} 个模型 · ${d.latency_ms} ms，点击模型框选择`, 'ok'); } catch (e) { res('pai-api-res', e.message, 'err'); } };
    $('#pai-latency').onclick = async () => { res('pai-api-res', '正在测速…'); try { const d = await j('/api/ai-latency', {body: apiPayload()}); res('pai-api-res', d.message || `${d.latency_ms} ms`, 'ok'); } catch (e) { res('pai-api-res', e.message, 'err'); } };

    // AI 回复设置（按平台）
    function setMsMode(mode) {
        document.querySelectorAll('input[name=pai-ms-mode]').forEach(r => { r.checked = r.value === mode; });
        $('.pai-ms').dataset.disabled = mode === 'global' ? '1' : '0';
    }
    function fillMs(ms) {
        ms = ms || {};
        $('#pai-ctx').value = String(ms.context_enabled !== false);
        $('#pai-win').value = ms.context_window || 6000;
        $('#pai-comp').value = String(ms.auto_compress !== false);
        $('#pai-words').value = (ms.prohibited_words || []).join('，');
    }
    let globalMs = {};
    document.querySelectorAll('input[name=pai-ms-mode]').forEach(r => r.addEventListener('change', () => { setMsMode(r.value); if (r.value === 'global') fillMs(globalMs); }));
    function noteAccount(d) {
        if (!isLaike) return;
        const who = (d && d.account_name) || '当前账号';
        const replyNote = document.getElementById('pai-reply-note');
        if (replyNote) replyNote.textContent = `只对来客账号「${who}」生效。其他人设、话术、二次触达和黑名单都互不影响。AI 接口密钥仍是全局共用的。`;
        const kbNote = document.getElementById('pai-kb-note');
        if (kbNote) kbNote.textContent = `「${who}」自己的知识库。新建或修改不会改到其他来客账号，也不会改到抖音私信或小红书。`;
    }
    async function loadReply(c) {
        globalMs = c.model_settings || {};
        const d = await j(isLaike ? '/api/laike-accounts/ai' : `/api/ai-platform-settings/${P}`);
        const s = d.settings || {};
        if (isLaike) {
            $('#pai-plat-on').checked = !!d.ai_enabled;
            noteAccount(d);
        }
        $('#pai-persona').value = s.persona || '';
        if (isLaike) {
            $('#pai-fu-on').checked = !!s.follow_up_enabled;
            $('#pai-fu-delay').value = s.follow_up_delay || 120;
            $('#pai-fu-skip').checked = s.skip_if_lead !== false;
            $('#pai-fu-card').checked = s.lead_card_on_follow_up !== false;
            $('#pai-fu-prompt').value = s.follow_up_prompt || '';
            $('#pai-bl').value = (s.blacklist || []).join('\n');
            $('#pai-rules').value = s.reply_rules || '';
            $('#pai-noq').value = s.no_question_fallback || '';
            $('#pai-thanks').value = (s.contact_thanks || []).join('\n');
            $('#pai-afterlead').value = (s.after_lead_replies || []).join('\n');
            $('#pai-adn').value = (s.ad_name_keywords || []).join('\n');
            $('#pai-adt').value = (s.ad_text_keywords || []).join('\n');
            $('#pai-ent').checked = s.block_enterprise !== false;
            $('#pai-loop').checked = s.loop_guard !== false;
            $('#pai-sg-on').checked = s.silent_greet_enabled !== false;
            $('#pai-sg-delay').value = s.silent_greet_delay ?? 0;
            $('#pai-sg-text').value = s.silent_greet_text || '在的，亲';
        }
        if (s.model_settings) { fillMs(s.model_settings); setMsMode('own'); } else { fillMs(globalMs); setMsMode('global'); }
    }
    const lines = sel => $(sel).value.split('\n').map(x => x.trim()).filter(Boolean);
    $('#pai-reply-save').onclick = async () => {
        res('pai-reply-res', '正在保存…');
        try {
            const ps = {persona: $('#pai-persona').value};
            if (isLaike) Object.assign(ps, {follow_up_enabled: $('#pai-fu-on').checked, follow_up_delay: Number($('#pai-fu-delay').value) || 120, skip_if_lead: $('#pai-fu-skip').checked, lead_card_on_follow_up: $('#pai-fu-card').checked, follow_up_prompt: $('#pai-fu-prompt').value, blacklist: $('#pai-bl').value.split('\n').map(x => x.trim()).filter(Boolean), reply_rules: $('#pai-rules').value,
                no_question_fallback: $('#pai-noq').value.trim(), block_enterprise: $('#pai-ent').checked, loop_guard: $('#pai-loop').checked,
                contact_thanks: lines('#pai-thanks'), after_lead_replies: lines('#pai-afterlead'), ad_name_keywords: lines('#pai-adn'), ad_text_keywords: lines('#pai-adt'),
                silent_greet_enabled: $('#pai-sg-on').checked, silent_greet_delay: Number($('#pai-sg-delay').value), silent_greet_text: $('#pai-sg-text').value.trim()});
            if (document.querySelector('input[name=pai-ms-mode]:checked')?.value === 'own') {
                ps.model_settings = {context_enabled: $('#pai-ctx').value === 'true', context_window: Number($('#pai-win').value) || 6000, auto_compress: $('#pai-comp').value === 'true', prohibited_words: $('#pai-words').value};
            } else ps.use_global_model_settings = true;
            if (isLaike) {
                ps.ai_enabled = $('#pai-plat-on').checked;
                await j('/api/laike-accounts/ai', {body: ps});
                await loadReply(await loadApi());
                res('pai-reply-res', '已保存到当前来客账号', 'ok');
                return;
            }
            const cur = await j('/api/ai-config');  // 取最新开关，避免覆盖其他平台
            const platforms = {...(cur.platforms || {}), [P]: $('#pai-plat-on').checked};
            await j('/api/ai-config', {body: {platforms}});
            await j(`/api/ai-platform-settings/${P}`, {body: ps});
            await loadReply(await loadApi());
            res('pai-reply-res', `已保存（仅 ${NAME}）`, 'ok');
        } catch (e) { res('pai-reply-res', e.message, 'err'); }
    };

    // 知识库
    let KB = {bases: [], platform_assignments: {}};
    function renderKb() {
        const chosen = new Set(KB.platform_assignments[P] || []);
        $('#pai-kb-on').checked = !!KB.enabled;
        $('#pai-kb-list').innerHTML = KB.bases.length ? KB.bases.map(b => `<label><input type="checkbox" value="${esc(b.id)}" ${chosen.has(b.id) ? 'checked' : ''}>${esc(b.name)}<small>${(b.text || '').length} 字</small></label>`).join('') : '<span class="help-text">暂无知识库，可在下方新建</span>';
        const sel = $('#pai-kb-sel'); const prev = sel.value;
        sel.innerHTML = KB.bases.map(b => `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join('') + '<option value="">＋ 新建知识库</option>';
        sel.value = KB.bases.some(b => b.id === prev) || prev === '' && sel.dataset.touched ? prev : (KB.bases[0]?.id || '');
        fillKbEditor();
    }
    function fillKbEditor() { const b = KB.bases.find(x => x.id === $('#pai-kb-sel').value); $('#pai-kb-name').value = b ? b.name : ''; $('#pai-kb-text').value = b ? (b.text || '') : ''; }
    $('#pai-kb-sel').onchange = () => { $('#pai-kb-sel').dataset.touched = '1'; fillKbEditor(); };
    async function loadKb() {
        KB = await j(isLaike ? '/api/laike-accounts/knowledge' : '/api/ai-knowledge');
        KB.platform_assignments = KB.platform_assignments || {};
        if (isLaike) noteAccount(KB);
        renderKb();
    }
    $('#pai-kb-save').onclick = async () => {
        const ids = [...document.querySelectorAll('#pai-kb-list input:checked')].map(i => i.value);
        res('pai-kb-res', '正在保存…');
        try {
            const url = isLaike ? '/api/laike-accounts/knowledge/settings' : '/api/ai-knowledge/settings';
            await j(url, {body: {enabled: $('#pai-kb-on').checked, platform_assignments: {[P]: ids}}});
            await loadKb();
            res('pai-kb-res', isLaike ? `已保存到当前来客账号（${ids.length} 个知识库）` : `已保存（${NAME} 使用 ${ids.length} 个知识库）`, 'ok');
        } catch (e) { res('pai-kb-res', e.message, 'err'); }
    };
    $('#pai-kb-text-save').onclick = async () => {
        const id = $('#pai-kb-sel').value; const b = KB.bases.find(x => x.id === id);
        if (b && (b.documents || []).length && !b.text && !confirm('该知识库内容来自上传的文档，保存后将改为在线文本，继续？')) return;
        res('pai-kb-text-res', '正在保存…');
        try {
            const url = isLaike ? '/api/laike-accounts/knowledge' : '/api/ai-knowledge';
            const d = await j(url, {body: {id, name: $('#pai-kb-name').value.trim(), text: $('#pai-kb-text').value}});
            $('#pai-kb-sel').dataset.touched = '';
            await loadKb();
            $('#pai-kb-sel').value = d.knowledge_base?.id || id;
            fillKbEditor();
            res('pai-kb-text-res', isLaike ? '知识库已保存到当前来客账号' : '知识库内容已保存（所有平台共用）', 'ok');
        } catch (e) { res('pai-kb-text-res', e.message, 'err'); }
    };

    // 人工待回
    $('#pai-ho-on').onchange = async e => {
        try { await j('/api/ai-config', {body: {human_handoff: {enabled: e.target.checked}}}); res('pai-ho-res', e.target.checked ? '已启用（所有平台）' : '已关闭（所有平台）', 'ok'); } catch (err) { e.target.checked = !e.target.checked; res('pai-ho-res', err.message, 'err'); }
    };
    async function loadHo() {
        try {
            const d = await j(`/api/ai-handoffs?platform=${encodeURIComponent(P)}`);
            let items = d.items || [];
            if (isLaike && window.laikeCurrentAccountId) {
                const id = window.laikeCurrentAccountId;
                items = items.filter(it => !it.laike_account_id || it.laike_account_id === id);
            }
            $('#pai-ho-list').innerHTML = items.length ? items.map(it => `<div class="pai-ho" data-id="${esc(it.id)}"><div class="meta">${esc(it.sender_name || '客户')} · ${esc(it.reason || '')}</div><div>${esc(it.message_text || '')}</div>
<div class="pai-row" style="margin-top:8px"><input type="text" placeholder="输入人工回复" style="flex:1;min-width:180px"><button class="btn-primary" type="button" data-act="reply">发送</button><button class="btn-secondary" type="button" data-act="dismiss">忽略</button></div></div>`).join('') : '<p class="help-text">暂无待处理消息</p>';
        } catch (e) { $('#pai-ho-list').innerHTML = `<p class="help-text">${esc(e.message)}</p>`; }
    }
    $('#pai-ho-refresh').onclick = loadHo;
    $('#pai-ho-list').addEventListener('click', async e => {
        const btn = e.target.closest('button[data-act]'); if (!btn) return;
        const box = btn.closest('.pai-ho'); const id = box.dataset.id;
        try {
            if (btn.dataset.act === 'reply') {
                const text = box.querySelector('input').value.trim(); if (!text) return res('pai-ho-res', '请输入回复内容', 'err');
                if (!confirm('确认把这条回复发送给客户？')) return;
                await j(`/api/ai-handoffs/${encodeURIComponent(id)}/reply`, {body: {text}});
            } else await j(`/api/ai-handoffs/${encodeURIComponent(id)}/dismiss`, {method: 'POST'});
            res('pai-ho-res', '已处理', 'ok'); loadHo();
        } catch (err) { res('pai-ho-res', err.message, 'err'); }
    });

    async function init() {
        try { const c = await loadApi(); await loadReply(c); } catch (e) { res('pai-api-res', '读取 AI 配置失败：' + e.message, 'err'); }
        loadKb().catch(e => { $('#pai-kb-list').textContent = '读取失败：' + e.message; });
        loadHo();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
    if (isLaike) window.addEventListener('laike-account-changed', () => { init(); });
})();
