"""从 douyin_laike.log 回填抖音来客仪表盘事件（幂等，可重复运行）。"""
import os, re, time
from datetime import datetime
from dashboard_metrics import record_dashboard_event

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'douyin_laike.log')
pat = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$')
counts = {}
last_in = {}
with open(LOG, encoding='utf-8', errors='ignore') as f:
    for n, line in enumerate(f):
        m = pat.match(line.rstrip('\n'))
        if not m:
            continue
        ts = int(time.mktime(datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timetuple()))
        msg = m.group(3)
        nm = re.search(r'\[([^\]]+)\]', msg)
        nick = nm.group(1) if nm else ''
        uk = f'laike:{nick}'
        key = f'backfill|{m.group(1)}|{msg[:80]}'
        ev = None
        if '新消息' in msg and msg.startswith('📩'):
            ev = ('inbound', '', None); last_in[uk] = ts
        elif '来客二次触达已发送' in msg:
            ev = ('reply_success', 'follow_up', None)
        elif '来客已回复' in msg:
            ev = ('reply_success', 'ai', float(ts - last_in[uk]) if uk in last_in else None)
        elif '来客发送失败' in msg or '发送异常' in msg:
            ev = ('reply_failure', 'ai', None)
        elif '留资' in msg and nick:
            record_dashboard_event('douyin_laike', 'lead', f'{uk}|lead', contact_id=uk, occurred_at=ts)
            counts['lead'] = counts.get('lead', 0) + 1
            continue
        if ev:
            if record_dashboard_event('douyin_laike', ev[0], key, contact_id=uk, reply_mode=ev[1], occurred_at=ts, value=ev[2]):
                counts[ev[0]] = counts.get(ev[0], 0) + 1
print('回填完成:', counts)
