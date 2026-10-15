// 抖音来客私信 - 前端控制脚本
let laikeLogTimer = null;
let laikeCurrentAccountId = '';
let laikeAccounts = [];

function setLaikeCurrentAccount(id, notify) {
    const next = id || '';
    const changed = next !== laikeCurrentAccountId;
    laikeCurrentAccountId = next;
    window.laikeCurrentAccountId = next;
    if ((notify || changed) && next) {
        window.dispatchEvent(new CustomEvent('laike-account-changed'));
    }
}

function laikeSelectedName() {
    const acc = laikeAccounts.find(a => a.id === laikeCurrentAccountId);
    return (acc && acc.name) || '当前账号';
}

function laikeShortName(name) {
    const text = name || '当前账号';
    return text.length > 10 ? text.slice(0, 10) + '…' : text;
}

function laikeFetch(url, options) {
    options = options ? Object.assign({}, options) : {};
    let finalUrl = url;
    if (laikeCurrentAccountId && !/[?&]account_id=/.test(finalUrl)) {
        finalUrl += (finalUrl.indexOf('?') >= 0 ? '&' : '?') + 'account_id=' + encodeURIComponent(laikeCurrentAccountId);
    }
    const method = String(options.method || 'GET').toUpperCase();
    if (method !== 'GET' && options.body && laikeCurrentAccountId) {
        try {
            const obj = JSON.parse(options.body);
            if (obj && typeof obj === 'object' && !Array.isArray(obj) && !obj.account_id) {
                obj.account_id = laikeCurrentAccountId;
                options.body = JSON.stringify(obj);
            }
        } catch (e) { /* 非 JSON 请求保持原样 */ }
    }
    return fetch(finalUrl, options);
}

function toggleLaikeTimingConfig() {
    const content = document.getElementById('laike-timing-config-content');
    const icon = document.getElementById('laike-timing-toggle-icon');
    if (content.style.display === 'none') {
        content.style.display = 'block';
        icon.textContent = '▲';
    } else {
        content.style.display = 'none';
        icon.textContent = '▼';
    }
}

async function startLaikeMonitoring() {
    try {
        const res = await laikeFetch('/api/laike-start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({}),
        });
        const data = await res.json();
        if (data.success) {
            showToast(`已开始监控「${laikeSelectedName()}」。其他已启动的账号会继续各自扫描`, 'success');
            updateLaikeButtons(true);
            startLaikeLogPolling();
        } else {
            showToast('启动失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('启动失败: ' + e.message, 'error');
    }
}

async function stopLaikeMonitoring() {
    try {
        const res = await laikeFetch('/api/laike-stop', {method: 'POST'});
        const data = await res.json();
        if (data.success) {
            const stoppedName = laikeSelectedName();
            await refreshLaikeAccountInfo();
            const still = laikeAccounts.some(a => a.monitoring);
            showToast(still ? `已停止「${stoppedName}」。其他账号继续监控` : `已停止「${stoppedName}」`, 'info');
            updateLaikeButtons(false);
            if (still) startLaikeLogPolling();
            else stopLaikeLogPolling();
        }
    } catch (e) {
        showToast('停止失败: ' + e.message, 'error');
    }
}

function updateLaikeButtons(running) {
    const start = document.getElementById('start-btn');
    const stop = document.getElementById('stop-btn');
    const multi = laikeAccounts.length > 1;
    if (start) {
        start.disabled = running;
        start.textContent = multi ? `开始监控「${laikeShortName(laikeSelectedName())}」` : '开始监控';
    }
    if (stop) {
        stop.disabled = !running;
        stop.textContent = multi ? `停止「${laikeShortName(laikeSelectedName())}」` : '停止监控';
    }
    const dot = document.getElementById('status-dot');
    const status = document.getElementById('status');
    const runningCount = laikeAccounts.filter(a => a.monitoring).length;
    if (running) {
        dot.style.color = '#52c41a';
        status.textContent = laikeAccounts.length > 1 ? `${Math.max(runningCount, 1)} 个账号监控中` : '监控中';
    } else if (runningCount > 0) {
        dot.style.color = '#52c41a';
        status.textContent = `${runningCount} 个账号监控中`;
    } else {
        dot.style.color = '#ccc';
        status.textContent = '未启动';
    }
}

async function saveLaikeConfig() {
    const groupid = document.getElementById('laike-groupid').value.trim();
    const bizViewId = document.getElementById('laike-bizview').value.trim();
    const headless = document.getElementById('laike-headless').checked;
    try {
        const res = await laikeFetch('/api/laike-config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({groupid, biz_view_id: bizViewId, headless}),
        });
        const data = await res.json();
        showToast(data.success ? '配置已保存' : '保存失败', data.success ? 'success' : 'error');
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
}

async function saveLaikeTimingConfig() {
    const interval = parseFloat(document.getElementById('laike-message-check-interval').value) || 3;
    try {
        const res = await laikeFetch('/api/laike-config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message_check_interval: interval}),
        });
        const data = await res.json();
        showToast(data.success ? '时间配置已保存' : '保存失败', data.success ? 'success' : 'error');
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
}

async function loadLaikeConfig() {
    try {
        const res = await laikeFetch('/api/laike-config');
        const data = await res.json();
        if (data.groupid) document.getElementById('laike-groupid').value = data.groupid;
        if (data.biz_view_id) document.getElementById('laike-bizview').value = data.biz_view_id;
        document.getElementById('laike-headless').checked = data.headless !== false;
        if (data.message_check_interval) {
            document.getElementById('laike-message-check-interval').value = data.message_check_interval;
        }
        // 显示当前配置的账号
        updateLaikeAccountDisplay(data);
    } catch (e) {}
}

function updateLaikeAccountDisplay(data) {
    const nameEl = document.getElementById('laike-account-display');
    const gidEl = document.getElementById('laike-groupid-display');
    if (!nameEl) return;
    const name = data.laike_account_name || data.account_name || '';
    const gid = data.groupid || data.configured_groupid || '';
    if (name || gid) {
        nameEl.textContent = name || '(未命名账号)';
        gidEl.textContent = gid ? `groupid: ${gid}` : '';
    } else {
        nameEl.textContent = '未设置';
        gidEl.textContent = '';
    }
}

let laikeAccountsCache = [];
async function loadLaikeAccounts() {
    const btn = document.getElementById('laike-load-accounts-btn');
    const select = document.getElementById('laike-account-select');
    if (btn) { btn.disabled = true; btn.textContent = '正在读取...'; }
    showToast('正在探测账号列表，请稍候...', 'info');
    try {
        const res = await laikeFetch('/api/laike-list-accounts', {method: 'POST'});
        const data = await res.json();
        if (data.accounts && data.accounts.length > 0) {
            laikeAccountsCache = data.accounts;
            refreshLaikeAccountInfo();
            select.innerHTML = '<option value="">-- 选择账号 --</option>' + data.accounts.map((a, i) => `<option value="${i}">${a.name} (${a.groupid})</option>`).join('');
            select.style.display = 'inline-block';
            showToast(`找到 ${data.accounts.length} 个账号，请选择`, 'success');
        } else {
            showToast('未找到账号: ' + (data.error || '请确保已登录'), 'error');
        }
    } catch (e) {
        showToast('读取失败: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '读取账号列表'; }
    }
}
async function onLaikeAccountSelect() {
    const select = document.getElementById('laike-account-select');
    const idx = parseInt(select.value, 10);
    if (isNaN(idx) || !laikeAccountsCache[idx]) return;
    const acc = laikeAccountsCache[idx];
    if (acc.name && !acc.current && !acc.name.startsWith('账号_')) {
        const r = await (await laikeFetch('/api/laike-select-store', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: acc.name})})).json();
        showToast(r.success ? `已切换到门店：${acc.name}` : ('切换门店失败: ' + (r.error || '')), r.success ? 'success' : 'error');
        refreshLaikeAccountInfo();
        return;
    }
    // 如果是自动生成的名称（如账号_029440），让用户输入中文名
    let displayName = acc.name;
    if (!displayName || displayName.startsWith('账号_')) {
        const input = prompt('请输入该账号的中文名称（比如：盛世百花礼宴酒店）：', '');
        if (input && input.trim()) {
            displayName = input.trim();
            acc.name = displayName;
        }
    }
    document.getElementById('laike-groupid').value = acc.groupid;
    if (acc.biz_view_id) document.getElementById('laike-bizview').value = acc.biz_view_id;
    try {
        const res = await laikeFetch('/api/laike-sync-account', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({groupid: acc.groupid, account_name: displayName, biz_view_id: acc.biz_view_id}),
        });
        const data = await res.json();
        if (data.success) {
            showToast(`已选择账号: ${displayName}`, 'success');
            updateLaikeAccountDisplay({account_name: displayName, groupid: acc.groupid});
        } else {
            showToast('保存失败: ' + (data.error || ''), 'error');
        }
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
}
async function syncLaikeAccount() {
    // 从输入框获取用户手动填入的 groupid 和账号名
    const groupid = (document.getElementById('laike-groupid').value || '').trim();
    const bizViewId = (document.getElementById('laike-bizview').value || '').trim();
    // 账号名：尝试从显示区域获取，或让用户在 prompt 输入
    let accountName = '';
    const displayEl = document.getElementById('laike-account-display');
    if (displayEl && displayEl.textContent && displayEl.textContent !== '未设置') {
        accountName = displayEl.textContent;
    }
    if (!groupid) {
        showToast('请先在 Group ID 输入框中填入目标账号的 groupid（从浏览器地址栏复制）', 'error');
        return;
    }
    // 如果还没有账号名，弹窗让用户输入
    if (!accountName || accountName === '(未命名账号)') {
        accountName = prompt('给这个账号起个名字（比如：盛世百花礼宴酒店）：', '') || '';
    }
    showToast('正在保存账号...', 'info');
    try {
        const res = await laikeFetch('/api/laike-sync-account', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({groupid, account_name: accountName, biz_view_id: bizViewId}),
        });
        const data = await res.json();
        if (data.success) {
            showToast(`已设置监控账号: ${data.account_name || data.groupid}`, 'success');
            updateLaikeAccountDisplay({account_name: data.account_name, groupid: data.groupid});
            loadLaikeConfig();
        } else {
            showToast('保存失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
}

let laikeLoginPollTimer = null;

// ── 抖音来客无头登录弹窗 ──
// life.douyin.com 官方不支持扫码登录（passport 返回「不能用该app扫码」），
// 这里驱动服务器上无头浏览器里的官方登录表单：手机号+短信验证码 / 密码 / Cookie 导入。
// 截图实时回显，可直接在截图上点击或拖动（处理滑块/点选验证码、选择门店）。
function ensureLaikeLoginModal() {
    let m = document.getElementById('laike-login-modal');
    if (m) return m;
    m = document.createElement('div');
    m.id = 'laike-login-modal';
    m.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;display:none;align-items:flex-start;justify-content:center;overflow:auto;padding:20px 0;';
    m.innerHTML = `
      <div style="background:#fff;border-radius:10px;padding:16px 18px;width:min(1000px,96vw);box-shadow:0 8px 30px rgba(0,0,0,.3);font-size:14px;color:#222">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <strong style="font-size:16px">登录抖音来客（life.douyin.com）</strong>
          <button id="lk-close" class="btn-secondary">关闭</button>
        </div>
        <div id="lk-status" style="margin:6px 0;color:#555">正在打开登录页…</div>
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin:8px 0;align-items:center">
          <input id="lk-phone" placeholder="手机号" style="padding:6px;width:150px">
          <button id="lk-send" class="btn-primary">发送验证码</button>
          <input id="lk-code" placeholder="短信验证码" style="padding:6px;width:120px">
          <button id="lk-submit" class="btn-primary">登录</button>
          <span style="color:#999">|</span>
          <button id="lk-refresh" class="btn-secondary">刷新截图</button>
          <button id="lk-reopen" class="btn-secondary">重新打开登录页</button>
          <label style="margin-left:6px"><input type="checkbox" id="lk-drag"> 拖动模式（滑块验证码）</label>
        </div>
        <details style="margin:4px 0">
          <summary style="cursor:pointer;color:#1677ff">其他方式：密码登录 / 导入 Cookie</summary>
          <div style="display:flex;gap:8px;margin:8px 0;flex-wrap:wrap">
            <input id="lk-acc" placeholder="手机号/邮箱" style="padding:6px;width:180px">
            <input id="lk-pwd" type="password" placeholder="密码" style="padding:6px;width:160px">
            <button id="lk-pwd-btn" class="btn-secondary">密码登录</button>
          </div>
          <textarea id="lk-cookies" placeholder="粘贴已登录浏览器中 life.douyin.com 的 Cookie（JSON 数组 或 k=v; k2=v2）" style="width:100%;height:60px"></textarea>
          <button id="lk-cookie-btn" class="btn-secondary" style="margin-top:4px">导入 Cookie</button>
        </details>
        <div style="color:#888;margin:4px 0">提示：出现滑块/点选验证码时，直接在下方截图上点击；滑块请勾选「拖动模式」后从滑块按住拖到缺口。</div>
        <div style="position:relative;border:1px solid #ddd;display:inline-block;max-width:100%">
          <img id="lk-shot" style="max-width:100%;display:block;cursor:crosshair;user-select:none" draggable="false">
        </div>
      </div>`;
    document.body.appendChild(m);
    const $ = id => m.querySelector('#' + id);
    $('lk-close').onclick = closeLaikeLoginModal;
    $('lk-refresh').onclick = () => laikeLoginCall('/api/laike-login/snapshot', null, 'GET');
    $('lk-reopen').onclick = () => laikeLoginCall('/api/laike-login/open', {});
    $('lk-send').onclick = () => {
        const phone = $('lk-phone').value.trim();
        if (!/^1\d{10}$/.test(phone)) { showToast('请输入11位手机号', 'error'); return; }
        laikeLoginCall('/api/laike-login/send-code', {phone});
    };
    $('lk-submit').onclick = () => {
        const code = $('lk-code').value.trim();
        if (!code) { showToast('请输入验证码', 'error'); return; }
        laikeLoginCall('/api/laike-login/submit-code', {code});
    };
    $('lk-pwd-btn').onclick = () => laikeLoginCall('/api/laike-login/password', {account: $('lk-acc').value.trim(), password: $('lk-pwd').value});
    $('lk-cookie-btn').onclick = () => laikeLoginCall('/api/laike-login/import-cookies', {cookies: $('lk-cookies').value});
    // 截图上的点击/拖动 → 转发到无头浏览器（按实际像素换算）
    const img = $('lk-shot');
    let down = null;
    const toPage = (e) => {
        const r = img.getBoundingClientRect();
        const sx = img.naturalWidth / r.width, sy = img.naturalHeight / r.height;
        return {x: (e.clientX - r.left) * sx, y: (e.clientY - r.top) * sy};
    };
    img.addEventListener('mousedown', e => { e.preventDefault(); down = toPage(e); });
    img.addEventListener('mouseup', e => {
        if (!down) return;
        const up = toPage(e);
        const start = down; down = null;
        const moved = Math.hypot(up.x - start.x, up.y - start.y) > 8;
        if ($('lk-drag').checked && moved) {
            laikeLoginCall('/api/laike-login/mouse', {action: 'drag', x: start.x, y: start.y, x2: up.x, y2: up.y});
        } else {
            laikeLoginCall('/api/laike-login/mouse', {action: 'click', x: up.x, y: up.y});
        }
    });
    return m;
}

let laikeLoginBusy = false;
let laikePollPromise = null;
let laikeActionBusy = false;
async function laikeLoginCall(url, body, method) {
    const m = ensureLaikeLoginModal();
    const st = m.querySelector('#lk-status');
    const isPoll = method === 'GET' && url.endsWith('/snapshot');
    if (isPoll) {
        if (laikeActionBusy || laikePollPromise) return;  // 用户操作优先，轮询让路
    } else {
        if (laikeActionBusy) { st.textContent = '⏳ 上一个操作还在处理中，请稍候…'; return; }
        laikeActionBusy = true;
        laikeLoginBusy = true;
        m.querySelectorAll('button').forEach(b => { if (b.id !== 'lk-close') b.disabled = true; });
        st.style.color = '#555';
        st.textContent = url.includes('submit-code') ? '⏳ 正在填写验证码并登录（最多约 15 秒）…' : '⏳ 处理中…';
        if (laikePollPromise) { try { await laikePollPromise; } catch (_) {} }
    }
    const opt = method === 'GET' ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})};
    const run = (async () => {
        const d = await (await laikeFetch(url, opt)).json();
        if (isPoll && d.busy) return d;
        if (isPoll && laikeActionBusy) { if (d.screenshot) m.querySelector('#lk-shot').src = 'data:image/png;base64,' + d.screenshot; return d; }
        renderLaikeLogin(d);
        return d;
    })();
    if (isPoll) laikePollPromise = run;
    try {
        return await run;
    } catch (e) {
        if (!isPoll) { st.textContent = '请求失败: ' + e.message; st.style.color = '#d4380d'; }
    } finally {
        if (isPoll) laikePollPromise = null;
        else {
            laikeActionBusy = false;
            laikeLoginBusy = false;
            m.querySelectorAll('button').forEach(b => { b.disabled = false; });
            setTimeout(() => laikeLoginCall('/api/laike-login/snapshot', null, 'GET'), 300);
        }
    }
}

function renderLaikeLogin(d) {
    const m = ensureLaikeLoginModal();
    const st = m.querySelector('#lk-status');
    if (d.screenshot) m.querySelector('#lk-shot').src = 'data:image/png;base64,' + d.screenshot;
    if (!d.success) { st.textContent = '❌ ' + (d.error || '操作失败'); st.style.color = '#d4380d'; return; }
    if (d.logged_in) {
        st.textContent = '✅ 登录成功' + (d.store_name ? `：${d.store_name}` : '') + (d.groupid ? `（groupid=${d.groupid}）` : '') + ((d.stores || []).length > 1 ? `，共 ${d.stores.length} 个门店，可在账号配置中选择` : '') + '，可关闭窗口并开始监控';
        st.style.color = '#389e0d';
        if (laikeLoginPollTimer) { clearInterval(laikeLoginPollTimer); laikeLoginPollTimer = null; }
        showToast('抖音来客登录成功' + (d.store_name ? `：${d.store_name}` : ''), 'success');
        refreshLaikeAccountInfo();
        return;
    }
    st.style.color = '#555';
    let msg = '未登录。请输入手机号获取验证码，或使用其他方式。';
    if (d.captcha) msg = '⚠️ 出现安全验证，请直接在截图上完成（点击/拖动）。';
    if (d.tips) msg += ' 页面提示：' + d.tips;
    if (d.message) {
        msg = (d.result === 'captcha' ? '⚠️ ' : (d.result === 'pending' || d.result === 'error' || d.result === 'fill_failed' || d.result === 'invalid' || d.result === 'no_input') ? '❌ ' : '') + d.message;
        st.style.color = d.result === 'captcha' ? '#d48806' : '#d4380d';
    }
    st.textContent = msg;
}

function closeLaikeLoginModal() {
    const m = document.getElementById('laike-login-modal');
    if (m) m.style.display = 'none';
    if (laikeLoginPollTimer) { clearInterval(laikeLoginPollTimer); laikeLoginPollTimer = null; }
}

function startLaikeLogin() {
    const m = ensureLaikeLoginModal();
    const acc = laikeAccounts.find(a => a.id === laikeCurrentAccountId);
    const title = m.querySelector('strong');
    if (title) title.textContent = '登录抖音来客' + (acc && acc.name ? `（${acc.name}）` : '');
    m.style.display = 'flex';
    if (laikeLoginPollTimer) clearInterval(laikeLoginPollTimer);
    laikeLoginCall('/api/laike-login/open', {}).then(() => {
        // 每 2 秒刷新截图与登录状态
        laikeLoginPollTimer = setInterval(() => {
            laikeLoginCall('/api/laike-login/snapshot', null, 'GET');
        }, 2000);
    });
}

function renderLaikeAccountList(data) {
    const box = document.getElementById('laike-account-list');
    if (!box) return;
    const accounts = (data && data.accounts) || [];
    laikeAccounts = accounts;
    if (!laikeCurrentAccountId) {
        setLaikeCurrentAccount((data && (data.account_id || data.active_id)) || (accounts[0] && accounts[0].id) || '', true);
    }
    const warn = document.getElementById('laike-memory-warning');
    if (warn) {
        const text = (data && data.memory_warning) || '';
        warn.style.display = text ? 'block' : 'none';
        warn.textContent = text;
    }
    const maxInput = document.getElementById('laike-max-browsers');
    if (maxInput && document.activeElement !== maxInput && data && data.max_concurrent_browsers) {
        maxInput.value = data.max_concurrent_browsers;
    }
    const usage = document.getElementById('laike-browser-usage');
    if (usage && data) {
        const ram = data.host_ram_mb ? `本机内存约 ${(data.host_ram_mb / 1024).toFixed(1)} GB。` : '';
        usage.textContent = `${ram}当前打开 ${data.browsers_running || 0} / ${data.max_concurrent_browsers || 1} 个来客浏览器。停止监控后，空闲约 5 分钟会关掉该账号的浏览器（登录保留）。`;
    }
    if (!accounts.length) {
        box.innerHTML = '<div style="padding:10px;background:#fafafa;border-radius:8px;color:#888">还没有来客账号。添加后会单独打开一个浏览器登录。</div>';
        return;
    }
    box.innerHTML = accounts.map(acc => {
        const selected = acc.id === laikeCurrentAccountId;
        return `<div style="padding:10px 12px;border:1px solid ${selected ? '#1f7cff' : '#eee'};border-radius:8px;background:${selected ? '#f3f8ff' : '#fff'}">
            <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
                <strong>${escapeHtml(acc.name || acc.id)}</strong>
                <span style="color:${acc.logged_in ? '#389e0d' : '#d48806'}">${acc.logged_in ? '已登录' : '未登录'}</span>
                <span style="color:${acc.monitoring ? '#389e0d' : '#888'}">${acc.monitoring ? '监控中' : '未监控'}</span>
                <span style="color:${acc.ai_enabled ? '#389e0d' : '#888'}">${acc.ai_enabled ? 'AI 开' : 'AI 关'}</span>
                <span style="color:#555">知识库 ${acc.knowledge_count || 0}</span>
                <span style="color:#888">${acc.browser_open ? '浏览器开着' : '浏览器已关闭'}</span>
                ${acc.store_name ? `<span style="color:#555">${escapeHtml(acc.store_name)}</span>` : ''}
                ${acc.groupid ? `<span style="color:#999;font-size:12px">groupid ${escapeHtml(acc.groupid)}</span>` : ''}
            </div>
            <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px">
                <button class="btn-secondary" type="button" onclick="laikeSelectAccount('${acc.id}')">${selected ? '当前账号' : '选中'}</button>
                <button class="btn-secondary" type="button" onclick="laikeLoginAccount('${acc.id}')">登录</button>
                <button class="btn-primary" type="button" onclick="laikeToggleAccount('${acc.id}', ${acc.monitoring ? 'false' : 'true'})">${acc.monitoring ? '停止此账号' : '开始此账号'}</button>
                <button class="btn-secondary" type="button" onclick="laikeRenameAccount('${acc.id}')">改名</button>
                <button class="btn-secondary" type="button" onclick="laikeRemoveAccount('${acc.id}')">删除</button>
            </div>
        </div>`;
    }).join('');
    const running = accounts.filter(a => a.monitoring).length;
    const status = document.getElementById('status');
    if (status && accounts.length > 1) {
        status.textContent = running ? `${running} 个账号监控中` : '未启动';
    }
    const copy = document.getElementById('laike-copy-from');
    if (copy) {
        const current = copy.value;
        copy.innerHTML = '<option value="">空白，不复制</option>' + accounts.map(acc =>
            `<option value="${acc.id}">复制「${escapeHtml(acc.name || acc.id)}」的 AI / 知识库 / 话术</option>`
        ).join('');
        if ([...copy.options].some(opt => opt.value === current)) copy.value = current;
    }
    updateLaikeButtons(!!(accounts.find(a => a.id === laikeCurrentAccountId) || {}).monitoring);
}

async function laikeSelectAccount(id) {
    setLaikeCurrentAccount(id, true);
    laikeLastLogKey = '';
    try {
        await laikeFetch('/api/laike-accounts/select', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id})});
    } catch (e) {}
    await refreshLaikeAccountInfo();
    loadLaikeConfig();
    refreshLaikeLogs();
}

async function laikeAddAccount() {
    const name = prompt('新来客账号的名称（例如门店名）：', '');
    if (!name || !name.trim()) return;
    try {
        const copyFrom = (document.getElementById('laike-copy-from') || {}).value || '';
        const data = await (await laikeFetch('/api/laike-accounts', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: name.trim(), copy_from: copyFrom}),
        })).json();
        if (!data.success) { showToast(data.error || '添加失败', 'error'); return; }
        setLaikeCurrentAccount((data.account && data.account.id) || '', true);
        showToast(copyFrom ? '已添加，并复制了所选账号的 AI / 知识库 / 话术。请单独登录' : '已添加。AI、知识库和话术是空白的，请单独登录并配置', 'success');
        await refreshLaikeAccountInfo();
        startLaikeLogin();
    } catch (e) {
        showToast('添加失败: ' + e.message, 'error');
    }
}

async function laikeRenameAccount(id) {
    const current = (laikeAccounts.find(a => a.id === id) || {}).name || '';
    const name = prompt('账号名称：', current);
    if (!name || !name.trim()) return;
    const data = await (await laikeFetch('/api/laike-accounts/rename', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id, name: name.trim()}),
    })).json();
    showToast(data.success ? '已改名' : (data.error || '改名失败'), data.success ? 'success' : 'error');
    refreshLaikeAccountInfo();
}

async function laikeRemoveAccount(id) {
    const current = (laikeAccounts.find(a => a.id === id) || {}).name || id;
    if (!confirm(`删除来客账号「${current}」？\n\n会停止它的监控并清掉这个账号自己的登录数据，其他账号不受影响。`)) return;
    const data = await (await laikeFetch('/api/laike-accounts/remove', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}),
    })).json();
    if (!data.success) { showToast(data.error || '删除失败', 'error'); return; }
    if (laikeCurrentAccountId === id) setLaikeCurrentAccount(data.active_id || '', true);
    showToast('已删除该来客账号', 'info');
    refreshLaikeAccountInfo();
    loadLaikeConfig();
}

async function laikeLoginAccount(id) {
    await laikeSelectAccount(id);
    startLaikeLogin();
}

async function laikeToggleAccount(id, start) {
    setLaikeCurrentAccount(id, true);
    if (start) await startLaikeMonitoring();
    else await stopLaikeMonitoring();
    refreshLaikeAccountInfo();
}

async function startAllLaike() {
    try {
        const data = await (await laikeFetch('/api/laike-start-all', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})).json();
        const failed = (data.results || []).filter(r => !r.success);
        if (failed.length) showToast(failed.map(r => (r.name || r.id) + ': ' + (r.error || '失败')).join('；'), 'error');
        else showToast(`已启动 ${data.started || 0} 个账号`, 'success');
        refreshLaikeAccountInfo();
        startLaikeLogPolling();
    } catch (e) {
        showToast('全部启动失败: ' + e.message, 'error');
    }
}

async function stopAllLaike() {
    try {
        const data = await (await laikeFetch('/api/laike-stop-all', {method: 'POST'})).json();
        showToast(data.success ? '已停止全部来客监控' : (data.error || '停止失败'), data.success ? 'info' : 'error');
        updateLaikeButtons(false);
        refreshLaikeAccountInfo();
    } catch (e) {
        showToast('停止失败: ' + e.message, 'error');
    }
}

async function saveLaikeBrowserLimit() {
    const value = parseInt(document.getElementById('laike-max-browsers').value, 10);
    const data = await (await laikeFetch('/api/laike-accounts/limits', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({max_concurrent_browsers: value}),
    })).json();
    showToast(data.success ? `同时浏览器上限已设为 ${data.max_concurrent_browsers}` : '保存失败', data.success ? 'success' : 'error');
    renderLaikeAccountList(data);
}

async function refreshLaikeAccountInfo() {
    try {
        const data = await (await laikeFetch('/api/laike-status')).json();
        renderLaikeAccountList(data);
        const nickEl = document.getElementById('account-nickname');
        const uidEl = document.getElementById('account-uid');
        const statusEl = document.getElementById('login-status-text');
        if (data.logged_in) {
            nickEl.textContent = (data.store_name || '已登录') + (data.user_name ? ` · ${data.user_name}` : '');
            uidEl.textContent = data.groupid ? `groupid ${data.groupid} · biz ${data.biz_view_id || '-'}` : 'life.douyin.com';
            statusEl.textContent = '已登录';
            statusEl.className = 'value ok';
        } else {
            nickEl.textContent = '未登录';
            uidEl.textContent = 'life.douyin.com';
            statusEl.textContent = '未登录';
            statusEl.className = 'value warn';
        }
        renderLaikeStores(data.stores || [], data.store_name || '');
        // 同步「系统控制」中的账号与 groupid / biz 字段
        const label = data.account_name
            ? (data.store_name && data.store_name !== data.account_name ? `${data.account_name} · ${data.store_name}` : data.account_name)
            : (data.store_name || '');
        updateLaikeAccountDisplay({laike_account_name: label, groupid: data.groupid || ''});
        const g = document.getElementById('laike-groupid'), bz = document.getElementById('laike-bizview');
        if (g && document.activeElement !== g) g.value = data.groupid || '';
        if (bz && document.activeElement !== bz) bz.value = data.biz_view_id || '';
    } catch (e) {}
}

function renderLaikeStores(stores, current) {
    const box = document.getElementById('laike-store-picker');
    if (!box) return;
    if (!stores || stores.length <= 1) { box.style.display = 'none'; return; }
    box.style.display = 'block';
    const sel = box.querySelector('select');
    sel.innerHTML = stores.map(n => `<option ${n === current ? 'selected' : ''}>${escapeHtml(n)}</option>`).join('');
}

async function laikeSelectStore() {
    const sel = document.querySelector('#laike-store-picker select');
    if (!sel) return;
    const d = await (await laikeFetch('/api/laike-select-store', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: sel.value})})).json();
    showToast(d.success ? `已切换门店：${d.store_name || sel.value}` : ('切换失败: ' + d.error), d.success ? 'success' : 'error');
    refreshLaikeAccountInfo();
}

async function laikeDetectAccount() {
    const d = await (await laikeFetch('/api/laike-detect-account', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})).json();
    if (!d.success) showToast('识别失败: ' + d.error, 'error');
    refreshLaikeAccountInfo();
}

async function laikeSwitchAccount() {
    if (!confirm('确定退出当前这个来客账号的登录吗？\n\n只会清空这个账号自己的浏览器会话，其他账号保持原样。退出后需要重新登录此账号。')) return;
    const d = await (await laikeFetch('/api/laike-switch-account', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})).json();
    if (!d.success) { showToast('切换失败: ' + (d.error || ''), 'error'); return; }
    if (typeof updateLaikeButtons === 'function') updateLaikeButtons(false);
    showToast('已退出旧账号，请登录新账号', 'info');
    refreshLaikeAccountInfo();
    startLaikeLogin();
}

function formatLaikeLiveStatus(st) {
    const name = st.account_name ? `【${st.account_name}】` : '';
    return `${name}${st.monitoring ? '🟢 监控中' : '⚪ 未监控'} ｜ 扫描 ${st.scan_count || 0} 次 ｜ 上次扫描 ${st.last_scan || '-'} ｜ 会话 ${st.conversations || 0} ｜ WebSocket ${st.ws_connected ? '已连接' : '未连接'}（帧 ${st.ws_frames || 0}）｜ 触发 ${st.trigger_count || 0} 次，最近 ${st.last_trigger || '-'}`;
}

let laikeLastLogKey = '';
async function refreshLaikeLogs() {
    try {
        const allLogs = document.getElementById('laike-logs-all');
        const logUrl = allLogs && allLogs.checked ? '/api/laike-logs?scope=all' : '/api/laike-logs';
        const res = await laikeFetch(logUrl);
        const data = await res.json();
        const line = document.getElementById('laike-live-status');
        if (line) {
            const rows = (allLogs && allLogs.checked && data.statuses && data.statuses.length) ? data.statuses : [data.status || {}];
            line.textContent = rows.map(formatLaikeLiveStatus).join('\n');
        }
        const container = document.getElementById('logs-container');
        if (!container) return;
        const logs = data.logs || [];
        const key = logs.length + '|' + (logs.length ? logs[logs.length - 1].time + logs[logs.length - 1].message + (logs[logs.length - 1].account_id || '') : '');
        if (key === laikeLastLogKey) return;   // 无新日志不重绘
        laikeLastLogKey = key;
        const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 40;
        container.innerHTML = logs.map(l =>
            `<div class="log-entry log-${l.level || 'info'}"><span class="log-time">${l.time}</span> <span style="color:#1f7cff">${escapeHtml(l.account_name || '')}</span> ${escapeHtml(l.message)}</div>`
        ).join('');
        if (atBottom || !container.dataset.init) { container.scrollTop = container.scrollHeight; container.dataset.init = '1'; }
    } catch (e) {}
}

function clearLaikeLogs() {
    const c = document.getElementById('logs-container');
    if (c) c.innerHTML = '';
}

function startLaikeLogPolling() {
    stopLaikeLogPolling();
    refreshLaikeLogs();
    laikeLogTimer = setInterval(refreshLaikeLogs, 1500);
}

function stopLaikeLogPolling() {
    if (laikeLogTimer) { clearInterval(laikeLogTimer); laikeLogTimer = null; }
}

function escapeHtml(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function showToast(msg, type) {
    const c = document.getElementById('toast-container');
    if (!c) { alert(msg); return; }
    const el = document.createElement('div');
    el.className = 'toast toast-' + (type || 'info');
    el.textContent = msg;
    c.appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

function openDocsPage() {
    location.href = '/docs?from=laike';
}

function togglePlatformSwitcher(event) {
    event.stopPropagation();
    document.getElementById('platform-switcher').classList.toggle('open');
}

document.addEventListener('click', function(event) {
    const switcher = document.getElementById('platform-switcher');
    if (switcher && !switcher.contains(event.target)) {
        switcher.classList.remove('open');
    }
});

// 页面加载时初始化
document.addEventListener('DOMContentLoaded', () => {
    laikeFetch('/api/laike-accounts').then(r => r.json()).then(data => {
        if (data && data.active_id) setLaikeCurrentAccount(data.active_id, true);
        renderLaikeAccountList(data);
    }).catch(() => {});
    loadLaikeConfig();
    refreshLaikeAccountInfo();
    laikeFetch('/api/laike-status').then(r => r.json()).then(d => {
        if (d && d.account_id && !laikeCurrentAccountId) setLaikeCurrentAccount(d.account_id, true);
        renderLaikeAccountList(d);
        updateLaikeButtons(!!d.monitoring);
        if ((d.accounts || []).some(a => a.monitoring) || d.monitoring) startLaikeLogPolling();
    }).catch(() => {});
    // 监听 tab 切换，切到日志 tab 时刷新
    const observer = new MutationObserver(() => {
        const logsPanel = document.querySelector('[data-platform-panel="logs"]');
        if (logsPanel && logsPanel.style.display !== 'none') {
            refreshLaikeLogs();
        }
    });
    observer.observe(document.body, {attributes: true, subtree: true, attributeFilter: ['style', 'class']});
});

// 暴露给 platform_tabs 的 tab 切换钩子
window.addEventListener('hashchange', () => {
    if (location.hash.includes('tab=logs')) {
        refreshLaikeLogs();
        // 确保轮询在运行
        laikeFetch('/api/laike-status').then(r => r.json()).then(d => {
            if (((d.accounts || []).some(a => a.monitoring) || d.monitoring) && !laikeLogTimer) startLaikeLogPolling();
        }).catch(() => {});
    }
});

// 定期同步账号信息（登录/切换账号后自动刷新系统控制字段）
setInterval(() => { if (typeof refreshLaikeAccountInfo === 'function') refreshLaikeAccountInfo(); }, 10000);
