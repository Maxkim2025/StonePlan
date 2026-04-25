#!/usr/bin/env python3
"""Stone Plan Dashboard Generator
扫描 stone-plan 目录下所有 md 文件，生成一个自包含的可视化仪表盘 HTML。
支持动态刷新：内置 HTTP 服务器，HTML 页面每 10 秒自动拉取最新数据。
"""

import os
import re
import json
import urllib.parse
import sys
import uuid
import cgi
import shutil
import signal
from datetime import datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_HTML = os.path.join(BASE_DIR, "dashboard.html")
SYNC_QUEUE_FILE = os.path.join(BASE_DIR, ".sync-queue.json")
PORT = 8765

def console_print(message=""):
    """Print text safely across non-UTF-8 terminals (e.g. Windows GBK)."""
    text = str(message)
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe_text)

# ─── 数据解析函数 ───────────────────────────────────────────────

def parse_frontmatter(content):
    """解析 YAML frontmatter，支持简单的嵌套结构，兼容缺少关闭 --- 的情况"""
    # Try standard frontmatter first
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if match:
        fm_text = match.group(1)
    elif content.startswith('---'):
        # No closing ---, find where frontmatter ends (first non-indented non-key line)
        lines = content.split('\n')
        fm_lines = []
        for i, line in enumerate(lines[1:], 1):  # Skip opening ---
            stripped = line.strip()
            if not stripped:
                if fm_lines:  # Empty line after content = end of frontmatter
                    break
                continue
            if not line.startswith(' ') and not line.startswith('\t') and not re.match(r'^[\w].*:', stripped) and not stripped.startswith('- '):
                break
            fm_lines.append(line)
        fm_text = '\n'.join(fm_lines)
    else:
        return {}

    fm = {}
    current_key = None
    for line in fm_text.strip().split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        # Check indentation (nested key)
        if (line.startswith('  ') or line.startswith('\t')) and current_key:
            if ':' in stripped:
                nk, nv = stripped.split(':', 1)
                nk = nk.strip()
                nv = nv.strip().strip('"').strip("'")
                if isinstance(fm.get(current_key), dict):
                    fm[current_key][nk] = nv
            elif stripped.startswith('- '):
                item = stripped[2:].strip().strip('"').strip("'")
                if isinstance(fm.get(current_key), dict):
                    fm.setdefault(current_key + '_list', []).append(item)
                elif isinstance(fm.get(current_key), list):
                    fm[current_key].append(item)
            continue
        if ':' in stripped:
            key, val = stripped.split(':', 1)
            key = key.strip()
            val = val.strip()
            if not val:
                current_key = key
                fm[key] = {}
            elif val.startswith('[') and val.endswith(']'):
                items = re.findall(r'"([^"]+)"', val)
                fm[key] = items
            elif val.startswith('- '):
                fm[key] = [val[2:].strip().strip('"').strip("'")]
                current_key = key
            else:
                fm[key] = val.strip('"').strip("'")
                current_key = key
    # Flatten time dict to top level for easy access
    if 'time' in fm and isinstance(fm['time'], dict):
        for k, v in fm['time'].items():
            fm[f'time_{k}'] = v
    return fm

def parse_body(content):
    # Try standard frontmatter removal first
    body = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, count=1, flags=re.DOTALL)
    if body == content:
        # No closing --- found, skip opening --- and all frontmatter lines until body starts
        lines = content.split('\n')
        # Find where frontmatter ends: first non-indented line that doesn't look like a key:value
        fm_end = 0
        for i, line in enumerate(lines):
            if i == 0 and line.strip() == '---':
                fm_end = i + 1
                continue
            if i <= fm_end and fm_end > 0:
                stripped = line.strip()
                if not stripped:
                    fm_end = i + 1
                    continue
                if line.startswith(' ') or line.startswith('\t'):
                    fm_end = i + 1
                    continue
                if re.match(r'^[\w][\w\-]*:', stripped) or stripped.startswith('- '):
                    fm_end = i + 1
                    continue
                break
        body = '\n'.join(lines[fm_end:]).lstrip('\n')
    return body

def extract_tasks(body):
    tasks = []
    for line in body.split('\n'):
        line = line.strip()
        match = re.match(r'^[-*]\s*\[([ xX])\]\s*(.*)', line)
        if match:
            tasks.append({'done': match.group(1).lower() == 'x', 'text': match.group(2).strip()})
    return tasks

def extract_tables(body):
    tables = []
    lines = body.split('\n')
    table_lines = []
    in_table = False
    for line in lines:
        if '|' in line and line.strip().startswith('|'):
            in_table = True
            table_lines.append(line)
        elif in_table:
            if table_lines:
                tables.append(table_lines)
            table_lines = []
            in_table = False
    if table_lines:
        tables.append(table_lines)
    result = []
    for tl in tables:
        rows = []
        for line in tl:
            cells = [c.strip() for c in line.split('|')[1:-1]]
            rows.append(cells)
        if len(rows) > 1:
            result.append({'headers': rows[0], 'rows': rows[1:]})
    return result

def extract_sections(body):
    """提取 ## 标题及其下的内容"""
    sections = {}
    current_title = '_top'
    current_lines = []
    for line in body.split('\n'):
        m = re.match(r'^(#{1,3})\s+(.*)', line)
        if m and m.group(1) == '##':
            if current_lines:
                sections[current_title] = '\n'.join(current_lines)
            current_title = m.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections[current_title] = '\n'.join(current_lines)
    return sections

def _parse_date_from_text(text):
    """从文本中提取 YYYY-MM-DD 日期"""
    if not text:
        return None
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', str(text))
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None

def _parse_plan_start_from_backward_table(table):
    """从倒推表的日期范围里提取起始日期（如 4/25~5/1）"""
    if not table or not table.get('rows'):
        return None
    year = datetime.now().year
    for row in table.get('rows', []):
        if len(row) < 2:
            continue
        period = str(row[1])
        m = re.search(r'(\d{1,2})/(\d{1,2})', period)
        if not m:
            continue
        month, day = int(m.group(1)), int(m.group(2))
        try:
            return datetime(year, month, day)
        except ValueError:
            continue
    return None

def _goal_snapshot_path():
    return os.path.join(BASE_DIR, "inspection", "goal-progress-snapshots.json")

def _load_goal_snapshots():
    path = _goal_snapshot_path()
    if not os.path.exists(path):
        return {'goals': {}}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict) and 'goals' in data:
                return data
    except Exception:
        pass
    return {'goals': {}}

def _save_goal_snapshots(data):
    path = _goal_snapshot_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _apply_goal_weekly_trend(goals):
    """为目标注入最近7天增量趋势，并持久化快照"""
    snapshots = _load_goal_snapshots()
    today_str = datetime.now().strftime('%Y-%m-%d')
    goals_map = snapshots.setdefault('goals', {})

    for g in goals:
        gid = g.get('id') or g.get('file') or g.get('title')
        if not gid:
            continue
        entry = goals_map.setdefault(gid, {'title': g.get('title', ''), 'history': []})
        entry['title'] = g.get('title', '')
        history = entry.setdefault('history', [])

        # Upsert today's snapshot
        today_item = {
            'date': today_str,
            'done': int(g.get('milestones_done', 0)),
            'total': int(g.get('milestones_total', 0)),
        }
        replaced = False
        for i, item in enumerate(history):
            if item.get('date') == today_str:
                history[i] = today_item
                replaced = True
                break
        if not replaced:
            history.append(today_item)

        # Keep history sorted and bounded
        history.sort(key=lambda x: x.get('date', ''))
        if len(history) > 120:
            del history[:-120]

        # Compute 7-day trend from nearest snapshot >= 7 days ago
        now_dt = datetime.now().date()
        today_done = today_item['done']
        weekly_delta = 0
        weekly_days = 0
        baseline_done = today_done
        for item in reversed(history):
            dstr = item.get('date')
            try:
                d = datetime.strptime(dstr, '%Y-%m-%d').date()
            except Exception:
                continue
            diff_days = (now_dt - d).days
            if diff_days >= 7:
                weekly_days = diff_days
                baseline_done = int(item.get('done', today_done))
                weekly_delta = today_done - baseline_done
                break

        # If no >=7-day sample, fallback to earliest known sample
        if weekly_days == 0 and history:
            first = history[0]
            try:
                fd = datetime.strptime(first.get('date', today_str), '%Y-%m-%d').date()
                weekly_days = max(1, (now_dt - fd).days)
                baseline_done = int(first.get('done', today_done))
                weekly_delta = today_done - baseline_done
            except Exception:
                weekly_days = 0
                weekly_delta = 0

        g['weekly_delta'] = weekly_delta
        g['weekly_days'] = weekly_days
        g['weekly_pace'] = round(weekly_delta / weekly_days, 3) if weekly_days > 0 else 0
        g['trend_ready'] = weekly_days >= 3

    _save_goal_snapshots(snapshots)

def _daily_metrics_path():
    return os.path.join(BASE_DIR, "inspection", "daily-metrics.json")

def _load_daily_metrics():
    path = _daily_metrics_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def _save_daily_metrics(data):
    os.makedirs(os.path.join(BASE_DIR, "inspection"), exist_ok=True)
    with open(_daily_metrics_path(), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def upsert_daily_metric(today_data):
    if not today_data:
        return
    metrics = _load_daily_metrics()
    item = {
        'date': today_data.get('date', datetime.now().strftime('%Y-%m-%d')),
        'done': int(today_data.get('done', 0)),
        'total': int(today_data.get('total', 0)),
        'pct': int(today_data.get('pct', 0)),
        'sleep_hours': str(today_data.get('sleep_hours', '')),
    }
    replaced = False
    for i, m in enumerate(metrics):
        if m.get('date') == item['date']:
            metrics[i] = item
            replaced = True
            break
    if not replaced:
        metrics.append(item)
    metrics.sort(key=lambda x: x.get('date', ''))
    if len(metrics) > 90:
        metrics = metrics[-90:]
    _save_daily_metrics(metrics)

def _guardian_state_path():
    return os.path.join(BASE_DIR, "inspection", "guardian-state.json")

def _load_guardian_state():
    path = _guardian_state_path()
    if not os.path.exists(path):
        return {'alerts': []}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                data.setdefault('alerts', [])
                return data
    except Exception:
        pass
    return {'alerts': []}

def _save_guardian_state(state):
    os.makedirs(os.path.join(BASE_DIR, "inspection"), exist_ok=True)
    with open(_guardian_state_path(), 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def _write_alert_file(alert):
    dt = datetime.now()
    alert_dir = os.path.join(BASE_DIR, "alerts", dt.strftime("%Y"), dt.strftime("%m"))
    os.makedirs(alert_dir, exist_ok=True)
    fname = f"alert-{dt.strftime('%Y-%m-%d')}-{alert['id']}.md"
    path = os.path.join(alert_dir, fname)
    content = f"""---
id: {alert['id']}
date: {dt.strftime('%Y-%m-%d')}
level: {alert.get('level', 'high')}
type: guardian
---

## 风险描述
{alert['risk']}

## 证据来源
{alert['evidence']}

## 建议动作
{alert['action']}

## 建议截止时间
{alert['deadline']}

## 未执行潜在代价
{alert['cost']}
"""
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path

def list_today_alerts():
    today = datetime.now().strftime('%Y-%m-%d')
    alert_dir = os.path.join(BASE_DIR, "alerts", datetime.now().strftime("%Y"), datetime.now().strftime("%m"))
    if not os.path.exists(alert_dir):
        return []
    items = []
    for f in sorted(os.listdir(alert_dir), reverse=True):
        if not f.endswith('.md') or today not in f:
            continue
        path = os.path.join(alert_dir, f)
        try:
            txt = open(path, 'r', encoding='utf-8').read()
        except Exception:
            continue
        risk = ''
        action = ''
        m1 = re.search(r'## 风险描述\s*\n([^\n]+)', txt)
        if m1:
            risk = m1.group(1).strip()
        m2 = re.search(r'## 建议动作\s*\n([^\n]+)', txt)
        if m2:
            action = m2.group(1).strip()
        items.append({
            'file': os.path.relpath(path, BASE_DIR).replace('\\', '/'),
            'risk': risk or '存在风险信号，请查看告警详情。',
            'action': action or '请查看告警文件并执行建议动作。',
        })
    return items

def run_guardian_checks(data):
    """按规则巡检并生成 alerts 文件（同一天同规则去重）"""
    today = datetime.now().strftime('%Y-%m-%d')
    state = _load_guardian_state()
    fired_today = set(x for x in state.get('alerts', []) if x.startswith(today + ':'))
    new_alert_keys = []
    created_files = []

    goals = data.get('goals', [])
    # 规则1：7天未推进高优先级长期目标
    stalled = [g for g in goals if g.get('status') == '进行中' and int(g.get('weekly_days', 0)) >= 7 and float(g.get('weekly_pace', 0)) <= 0]
    if stalled:
        key = f"{today}:stalled-goals"
        if key not in fired_today:
            titles = '、'.join(g.get('title', '') for g in stalled[:4])
            alert = {
                'id': 'stalled-goals',
                'level': 'high',
                'risk': f"连续7天未推进进行中目标：{titles}",
                'evidence': "sources: goals/active/*.md + inspection/goal-progress-snapshots.json",
                'action': "明天中午前为每个停滞目标安排1个最小可执行动作（<=30分钟），并在今日任务中落地。",
                'deadline': (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d 12:00'),
                'cost': "长期目标继续停滞，截止风险快速上升，信心与执行惯性下降。"
            }
            created_files.append(_write_alert_file(alert))
            new_alert_keys.append(key)

    # 规则2：同类阻塞重复 >=3 次（基于 quicklog 文本）
    logs = data.get('quicklog', []) or []
    blocker_lines = [str(l.get('record', '')) for l in logs if any(k in str(l.get('record', '')) for k in ['失败', '阻塞', '卡住', '没做完'])]
    if len(blocker_lines) >= 3:
        key = f"{today}:repeat-blockers"
        if key not in fired_today:
            alert = {
                'id': 'repeat-blockers',
                'level': 'high',
                'risk': "同类阻塞信号累计达到 3 次以上，存在重复踩坑。",
                'evidence': "sources: quicklog/*.md recent records include 失败/阻塞/卡住",
                'action': "今晚写一份阻塞复盘清单（触发条件/解决动作/预防动作），并在明日任务中加入1条预防任务。",
                'deadline': (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d 21:00'),
                'cost': "问题将反复出现，执行效率下降，计划可信度被削弱。"
            }
            created_files.append(_write_alert_file(alert))
            new_alert_keys.append(key)

    # 规则3：计划完成率连续2周低于40%
    metrics = _load_daily_metrics()
    if len(metrics) >= 14:
        last14 = metrics[-14:]
        avg_pct = sum(int(m.get('pct', 0)) for m in last14) / len(last14)
        if avg_pct < 40:
            key = f"{today}:low-completion-2w"
            if key not in fired_today:
                alert = {
                    'id': 'low-completion-2w',
                    'level': 'high',
                    'risk': f"最近14天平均完成率仅 {avg_pct:.1f}%，低于40%阈值。",
                    'evidence': "sources: inspection/daily-metrics.json last 14 entries",
                    'action': "本周进行重规划：每日任务缩减为3个关键动作，保留一个兜底任务，先恢复完成率到60%。",
                    'deadline': (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d 22:00'),
                    'cost': "长期低完成会导致计划失真，目标持续延期并引发挫败累积。"
                }
                created_files.append(_write_alert_file(alert))
                new_alert_keys.append(key)

    # 规则4：健康关键信号异常（简单阈值）
    today_data = data.get('today') or {}
    sleep_raw = str(today_data.get('sleep_hours', '')).strip()
    try:
        sleep_hours = float(sleep_raw)
    except Exception:
        sleep_hours = None
    if sleep_hours is not None and (sleep_hours < 5.5 or sleep_hours > 10.5):
        key = f"{today}:sleep-anomaly"
        if key not in fired_today:
            alert = {
                'id': 'sleep-anomaly',
                'level': 'high',
                'risk': f"睡眠时长异常（{sleep_hours}h），超出健康阈值。",
                'evidence': "sources: tasks/daily/* frontmatter sleep_hours",
                'action': "明天优先修复作息：固定上床和起床时间，减少夜间高刺激活动。",
                'deadline': (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d 23:00'),
                'cost': "认知和执行稳定性下降，任务完成率和情绪波动风险升高。"
            }
            created_files.append(_write_alert_file(alert))
            new_alert_keys.append(key)

    if new_alert_keys:
        state['alerts'] = sorted(set(state.get('alerts', []) + new_alert_keys))
        if len(state['alerts']) > 500:
            state['alerts'] = state['alerts'][-500:]
        _save_guardian_state(state)
    return created_files

def write_daily_review(summary):
    """把每日总结落盘到 reviews 目录"""
    date_str = summary.get('date') or datetime.now().strftime('%Y-%m-%d')
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
    except Exception:
        dt = datetime.now()
        date_str = dt.strftime('%Y-%m-%d')
    review_dir = os.path.join(BASE_DIR, "reviews", dt.strftime("%Y"), dt.strftime("%m"))
    os.makedirs(review_dir, exist_ok=True)
    path = os.path.join(review_dir, f"review-daily-{date_str}.md")
    lines = [
        f"---",
        f"id: review-daily-{date_str}",
        f"date: {date_str}",
        f"type: daily",
        f"---",
        "",
        "## 结论",
        f"今天完成 {summary.get('done', 0)}/{summary.get('total', 0)}，XP {summary.get('xp', 0)}/{summary.get('total_xp', 0)}，连击 {summary.get('streak', 0)} 天。",
        "",
        "## 已知事实",
        f"- 完成任务: {summary.get('done', 0)}/{summary.get('total', 0)}",
        f"- XP: {summary.get('xp', 0)}/{summary.get('total_xp', 0)}",
        f"- 连击: {summary.get('streak', 0)} 天",
        "",
        "## 推断",
        "- 若明日优先处理未完成的首要任务，可提升连续完成稳定性。",
        "",
        "## 待确认",
        "- 今日阻塞项是否已拆解为明日最小行动。",
        "",
        "## 明日动作",
        f"- {summary.get('next_action', '先完成一项最小关键动作。')}",
        "",
        "## 来源",
        "- sources: /api/data (today, goals, quicklog)",
    ]
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return path

def write_failure_review(payload):
    """写入失败复盘到 reviews，并同步一条 quicklog"""
    date_str = payload.get('date') or datetime.now().strftime('%Y-%m-%d')
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
    except Exception:
        dt = datetime.now()
        date_str = dt.strftime('%Y-%m-%d')

    task_text = str(payload.get('task', '')).strip() or '未命名任务'
    slot = str(payload.get('slot', '')).strip()
    reason = str(payload.get('reason', '')).strip() or '未填写'
    action = str(payload.get('action', '')).strip() or '未填写'
    now = datetime.now().strftime('%H:%M')
    rid = f"failure-{dt.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"

    review_dir = os.path.join(BASE_DIR, "reviews", dt.strftime("%Y"), dt.strftime("%m"))
    os.makedirs(review_dir, exist_ok=True)
    review_path = os.path.join(review_dir, f"review-failure-{date_str}-{rid[-6:]}.md")
    review_content = f"""---
id: {rid}
date: {date_str}
time: {now}
type: failure-review
---

## 结论
任务未按计划完成，需要立即补一条可执行改进动作。

## 已知事实
- 任务: {task_text}
- 时间窗: {slot or '未提供'}
- 失败原因: {reason}
- 改进动作: {action}

## 推断
- 阻塞若不拆解，会在后续重复出现并拖慢完成率。

## 待确认
- 改进动作是否已写入明日任务。

## 明日动作
- {action}

## 来源
- source: dashboard /api/save-failure-review
"""
    with open(review_path, 'w', encoding='utf-8') as f:
        f.write(review_content)

    ql_dir = os.path.join(BASE_DIR, "quicklog", dt.strftime("%Y"), dt.strftime("%m"))
    os.makedirs(ql_dir, exist_ok=True)
    ql_path = os.path.join(ql_dir, f"quicklog-failure-{date_str}-{rid[-6:]}.md")
    ql_content = f"""---
id: {rid}
date: {date_str}
time: {now}
type: task
---

## 记录
- [{now}] 任务失败复盘：{task_text} | 原因：{reason} | 改进：{action}
"""
    with open(ql_path, 'w', encoding='utf-8') as f:
        f.write(ql_content)

    return {
        'ok': True,
        'review_path': review_path,
        'quicklog_path': ql_path,
        'id': rid,
    }

# ─── 扫描函数 ───────────────────────────────────────────────────

def scan_goals():
    goals = []
    goals_dir = os.path.join(BASE_DIR, "goals/active")
    if not os.path.exists(goals_dir):
        return goals
    for f in sorted(os.listdir(goals_dir)):
        if not f.endswith('.md'):
            continue
        path = os.path.join(goals_dir, f)
        content = open(path, encoding='utf-8').read()
        fm = parse_frontmatter(content)
        body = parse_body(content)
        tasks = extract_tasks(body)
        tables = extract_tables(body)
        sections = extract_sections(body)

        done = sum(1 for t in tasks if t['done'])
        total = len(tasks)

        deadline = fm.get('time_deadline', fm.get('deadline', '未设定'))
        remaining = fm.get('time_remaining_days', fm.get('remaining_days', '?'))
        progress = fm.get('progress', '0%')

        if 'completed' in fm.get('status', ''):
            status_color, status_text = '#10b981', '已完成'
        elif 'paused' in fm.get('status', ''):
            status_color, status_text = '#f59e0b', '暂停'
        else:
            status_color, status_text = '#3b82f6', '进行中'

        title = fm.get('title', f)
        emoji = '🎯'
        for e in ['🏃', '📖', '🤖', '💪', '📚', '💰']:
            if e in title:
                emoji = e
                break

        # Extract key metrics from sections
        metrics = {}
        for key, label in [('当前状态', '当前'), ('量化指标', '指标'), ('倒推计划', '倒推')]:
            if key in sections:
                metrics[label] = sections[key][:300]

        # Extract backward plan table
        backward_table = None
        for t in tables:
            if t['headers'] and any('周' in h for h in t['headers']):
                backward_table = t
                break
        deadline_dt = _parse_date_from_text(deadline)
        plan_start_dt = _parse_plan_start_from_backward_table(backward_table)

        goals.append({
            'id': fm.get('id', f.replace('.md', '')),
            'title': title,
            'emoji': emoji,
            'status': status_text,
            'status_color': status_color,
            'deadline': deadline,
            'remaining': remaining,
            'progress': progress,
            'milestones_done': done,
            'milestones_total': total,
            'milestone_pct': round(done / total * 100) if total > 0 else 0,
            'file': f,
            'tags': fm.get('tags_list', fm.get('tags', [])),
            'tasks': tasks,
            'tables': tables,
            'backward_table': backward_table,
            'metrics': metrics,
            'deadline_iso': deadline_dt.strftime('%Y-%m-%d') if deadline_dt else '',
            'plan_start_iso': plan_start_dt.strftime('%Y-%m-%d') if plan_start_dt else '',
        })
    return goals

def scan_today_tasks():
    today = datetime.now().strftime('%Y-%m-%d')
    today_path = os.path.join(BASE_DIR, f"tasks/daily/{today[:4]}/{today[5:7]}/{today}.md")

    if not os.path.exists(today_path):
        daily_dir = os.path.join(BASE_DIR, "tasks/daily")
        if not os.path.exists(daily_dir):
            return None
        all_files = []
        for root, dirs, files in os.walk(daily_dir):
            for f in files:
                if f.endswith('.md'):
                    all_files.append(os.path.join(root, f))
        if not all_files:
            return None
        all_files.sort(reverse=True)
        today_path = all_files[0]

    content = open(today_path, encoding='utf-8').read()
    fm = parse_frontmatter(content)
    body = parse_body(content)
    checklist_tasks = extract_tasks(body)
    tables = extract_tables(body)

    time_table = None
    for t in tables:
        if t['headers'] and any('时间' in str(h) for h in t['headers']):
            time_table = t
            break

    # Extract tasks from time table (status column has ⬜/✅)
    table_tasks = []
    if time_table:
        status_idx = None
        task_idx = None
        for i, h in enumerate(time_table['headers']):
            if '状态' in h:
                status_idx = i
            if '任务' in h:
                task_idx = i
        if status_idx is not None and task_idx is not None:
            for row in time_table['rows']:
                if len(row) > max(status_idx, task_idx):
                    task_text = row[task_idx]
                    # Skip separator rows and empty tasks
                    if task_text and not task_text.startswith('-'):
                        status = row[status_idx]
                        table_tasks.append({
                            'done': '✅' in status,
                            'text': task_text,
                            'slot': row[0] if len(row) > 0 else '',
                        })

    # Use table tasks if available, fallback to checklist
    tasks = table_tasks if table_tasks else checklist_tasks
    done = sum(1 for t in tasks if t['done'])
    total = len(tasks)

    return {
        'date': os.path.basename(today_path).replace('.md', ''),
        'energy': fm.get('energy_level', '中'),
        'sleep_time': fm.get('sleep_time', '-'),
        'wake_time': fm.get('wake_time', '-'),
        'sleep_hours': fm.get('sleep_hours', '-'),
        'tasks': tasks,
        'done': done,
        'total': total,
        'pct': round(done / total * 100) if total > 0 else 0,
        'time_table': time_table,
    }

def scan_quicklog():
    logs = []
    ql_dir = os.path.join(BASE_DIR, "quicklog")
    if not os.path.exists(ql_dir):
        return logs
    all_files = []
    for root, dirs, files in os.walk(ql_dir):
        for f in files:
            if f.endswith('.md'):
                all_files.append(os.path.join(root, f))
    all_files.sort(reverse=True)
    for path in all_files[:15]:
        content = open(path, encoding='utf-8').read()
        fm = parse_frontmatter(content)
        body = parse_body(content)
        record = ''
        for line in body.split('\n'):
            line = line.strip()
            if line.startswith('## 记录'):
                continue
            if line.startswith('##') or line.startswith('---'):
                break
            if line and not line.startswith('#'):
                record = line
                break
        logs.append({
            'id': fm.get('id', ''),
            'date': fm.get('date', ''),
            'time': fm.get('time', ''),
            'type': fm.get('type', ''),
            'record': record,
        })
    return logs

def scan_health():
    health = {}
    for name in ['weight', 'exercise', 'sleep', 'diet']:
        path = os.path.join(BASE_DIR, f"health/{name}.md")
        if os.path.exists(path):
            content = open(path, encoding='utf-8').read()
            tables = extract_tables(parse_body(content))
            health[name] = tables
    return health

def scan_finance():
    finance = {}
    for name in ['expense', 'budget', 'income']:
        path = os.path.join(BASE_DIR, f"finance/{name}.md")
        if os.path.exists(path):
            content = open(path, encoding='utf-8').read()
            tables = extract_tables(parse_body(content))
            finance[name] = tables
    return finance

def scan_weekly_plan():
    plan_dir = os.path.join(BASE_DIR, "plans/weekly")
    if not os.path.exists(plan_dir):
        return None
    all_files = sorted([f for f in os.listdir(plan_dir) if f.endswith('.md')], reverse=True)
    if not all_files:
        return None
    path = os.path.join(plan_dir, all_files[0])
    content = open(path, encoding='utf-8').read()
    fm = parse_frontmatter(content)
    body = parse_body(content)
    tables = extract_tables(body)
    return {
        'id': fm.get('id', ''),
        'period': fm.get('period', ''),
        'file': all_files[0],
        'tables': tables,
    }

def scan_memory():
    """扫描失败记忆"""
    path = os.path.join(BASE_DIR, "memory/patterns.md")
    if not os.path.exists(path):
        return []
    content = open(path, encoding='utf-8').read()
    tasks = extract_tasks(parse_body(content))
    return tasks

# ─── JSON API 数据生成 ──────────────────────────────────────────

def collect_data():
    """收集所有数据为 JSON"""
    goals = scan_goals()
    today = scan_today_tasks()
    quicklog = scan_quicklog()
    health = scan_health()
    finance = scan_finance()
    weekly_plan = scan_weekly_plan()
    memory = scan_memory()
    _apply_goal_weekly_trend(goals)
    upsert_daily_metric(today)
    guardian_files = run_guardian_checks({
        'goals': goals,
        'today': today,
        'quicklog': quicklog,
    })
    guardian_alerts = list_today_alerts()
    return {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'goals': goals,
        'today': today,
        'quicklog': quicklog,
        'health': health,
        'finance': finance,
        'weekly_plan': weekly_plan,
        'memory': memory,
        'guardian_alerts_created': guardian_files,
        'guardian_alerts': guardian_alerts,
    }

# ─── HTML 生成 ──────────────────────────────────────────────────

def generate_html():
    """生成自包含的 dashboard.html，通过 fetch 动态拉取 JSON 数据"""
    data = collect_data()

    # 首次生成时直接嵌入数据，之后通过 fetch 更新
    initial_json = json.dumps(data, ensure_ascii=False, indent=2)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🪨 Stone Plan Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
    background: #0f172a;
    color: #e2e8f0;
    min-height: 100vh;
  }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}

  /* Header */
  .header {{
    text-align: center;
    margin-bottom: 24px;
    position: relative;
  }}
  .header h1 {{
    font-size: 32px;
    margin-bottom: 4px;
    background: linear-gradient(135deg, #60a5fa, #a78bfa, #f472b6);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    display: inline-block;
  }}
  .header .subtitle {{
    color: #64748b;
    font-size: 14px;
  }}
  .header .live-dot {{
    display: inline-block;
    width: 8px; height: 8px;
    background: #10b981;
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 2s infinite;
  }}
  .header .update-time {{
    color: #475569;
    font-size: 12px;
    margin-top: 4px;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.3; }}
  }}

  /* Layout */
  .grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 20px;
  }}
  .grid-3 {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 20px;
    margin-bottom: 20px;
  }}
  .card {{
    background: #1e293b;
    border-radius: 12px;
    padding: 20px;
    border: 1px solid #334155;
    transition: border-color 0.3s;
  }}
  .card:hover {{ border-color: #475569; }}
  .card-title {{
    font-size: 15px;
    font-weight: 600;
    margin-bottom: 15px;
    display: flex;
    align-items: center;
    gap: 8px;
    color: #f1f5f9;
  }}
  .card-title .icon {{ font-size: 18px; }}
  .full-width {{ grid-column: 1 / -1; }}

  /* Stats bar */
  .stats-bar {{
    display: flex;
    gap: 16px;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }}
  .stat-item {{
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 12px;
    padding: 16px 24px;
    flex: 1;
    min-width: 160px;
    text-align: center;
  }}
  .stat-value {{
    font-size: 28px;
    font-weight: 700;
    background: linear-gradient(135deg, #60a5fa, #a78bfa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }}
  .stat-value.xp {{
    background: linear-gradient(135deg, #22d3ee, #3b82f6);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }}
  .stat-value.streak {{
    background: linear-gradient(135deg, #f59e0b, #ef4444);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }}
  .stat-label {{
    font-size: 12px;
    color: #64748b;
    margin-top: 4px;
  }}

  /* Goals */
  .goals-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 16px;
  }}
  .goal-card {{
    background: #1e293b;
    border-radius: 12px;
    padding: 16px;
    border: 1px solid #334155;
    border-left: 4px solid #3b82f6;
    transition: transform 0.2s, border-color 0.2s;
  }}
  .goal-card:hover {{ transform: translateY(-2px); border-color: #475569; }}
  .goal-header {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
  }}
  .goal-emoji {{ font-size: 28px; }}
  .goal-title {{ font-size: 15px; font-weight: 600; flex: 1; color: #f1f5f9; }}
  .goal-status {{
    font-size: 11px;
    padding: 2px 10px;
    border-radius: 10px;
    color: white;
    font-weight: 500;
  }}
  .goal-meta {{
    display: flex;
    gap: 16px;
    font-size: 12px;
    color: #94a3b8;
    margin-bottom: 10px;
    flex-wrap: wrap;
  }}
  .goal-tags {{ margin-top: 8px; }}
  .goal-forecast {{
    margin: 8px 0 2px 0;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
    font-size: 11px;
  }}
  .goal-forecast .chip {{
    background: #273449;
    color: #bfdbfe;
    border-radius: 8px;
    padding: 5px 8px;
  }}
  .goal-forecast .chip.risk-high {{ background: rgba(239,68,68,0.2); color: #fca5a5; }}
  .goal-forecast .chip.risk-mid {{ background: rgba(245,158,11,0.2); color: #fcd34d; }}
  .goal-forecast .chip.risk-low {{ background: rgba(16,185,129,0.2); color: #86efac; }}
  .deadline-chip {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
  }}
  .deadline-chip.red {{ background: rgba(239, 68, 68, 0.18); color: #fca5a5; }}
  .deadline-chip.yellow {{ background: rgba(245, 158, 11, 0.18); color: #fcd34d; }}
  .deadline-chip.blue {{ background: rgba(59, 130, 246, 0.18); color: #93c5fd; }}
  .deadline-chip.gray {{ background: rgba(148, 163, 184, 0.18); color: #cbd5e1; }}
  .deadline-chip.overdue {{ background: rgba(220, 38, 38, 0.25); color: #fecaca; }}
  .tag {{
    display: inline-block;
    background: #334155;
    padding: 2px 8px;
    border-radius: 8px;
    font-size: 11px;
    color: #94a3b8;
    margin-right: 4px;
  }}

  /* Progress bar */
  .progress-bar {{
    height: 6px;
    background: #334155;
    border-radius: 3px;
    overflow: hidden;
    margin-bottom: 10px;
  }}
  .progress-bar.large {{ height: 10px; }}
  .progress-fill {{
    height: 100%;
    border-radius: 3px;
    background: linear-gradient(90deg, #3b82f6, #60a5fa);
    transition: width 0.5s ease;
  }}
  .progress-fill.green {{ background: linear-gradient(90deg, #10b981, #34d399); }}
  .progress-fill.red {{ background: linear-gradient(90deg, #ef4444, #f87171); }}
  .progress-fill.amber {{ background: linear-gradient(90deg, #f59e0b, #fbbf24); }}
  .progress-label {{
    font-size: 12px;
    color: #94a3b8;
    margin-bottom: 4px;
  }}
  .progress-section {{ margin: 10px 0; }}
  .progress-fill.pulse {{ animation: pulseGrow 0.8s ease; }}
  @keyframes pulseGrow {{
    0% {{ filter: brightness(1); }}
    50% {{ filter: brightness(1.4); }}
    100% {{ filter: brightness(1); }}
  }}

  /* Milestones */
  .milestones {{ margin-top: 8px; }}
  .ms-item {{
    font-size: 12px;
    padding: 2px 0;
    color: #cbd5e1;
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    border-radius: 4px;
    padding: 3px 6px;
    margin: 1px 0;
    transition: background 0.2s;
    user-select: none;
  }}
  .ms-item:hover {{ background: #334155; }}
  .ms-item:active {{ transform: scale(0.98); }}
  .ms-item.done {{ color: #475569; text-decoration: line-through; }}
  .ms-item.syncing {{ opacity: 0.5; pointer-events: none; }}

  /* Tasks */
  .task-list {{ margin-top: 10px; }}
  .task-item {{
    font-size: 13px;
    padding: 6px 10px;
    min-height: 44px;
    color: #cbd5e1;
    border-radius: 6px;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    transition: background 0.2s;
    user-select: none;
    -webkit-tap-highlight-color: transparent;
  }}
  .task-item:hover {{ background: #334155; }}
  .task-item:active {{ transform: scale(0.98); }}
  .task-item.done {{ color: #475569; text-decoration: line-through; }}
  .task-item.syncing {{ opacity: 0.5; pointer-events: none; }}
  .task-item .check-icon {{ transition: transform 0.2s; }}
  .task-item:hover .check-icon {{ transform: scale(1.2); }}
  .task-main {{ flex: 1; }}
  .task-deadline {{
    font-size: 11px;
    color: #93c5fd;
    background: rgba(59,130,246,0.18);
    border: 1px solid rgba(96,165,250,0.35);
    border-radius: 8px;
    padding: 2px 8px;
    white-space: nowrap;
  }}
  .fail-btn {{
    border: 0;
    border-radius: 8px;
    padding: 4px 8px;
    font-size: 11px;
    color: #fecaca;
    background: rgba(127,29,29,0.45);
    cursor: pointer;
    white-space: nowrap;
  }}
  .fail-btn:hover {{ background: rgba(153, 27, 27, 0.55); }}
  .today-info {{
    display: flex;
    gap: 20px;
    font-size: 13px;
    color: #94a3b8;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }}
  .today-info span {{
    background: #334155;
    padding: 4px 12px;
    border-radius: 8px;
  }}
  .now-action {{
    background: linear-gradient(135deg, rgba(59,130,246,0.18), rgba(16,185,129,0.18));
    border: 1px solid rgba(96,165,250,0.35);
    border-radius: 12px;
    padding: 12px 14px;
    margin-bottom: 12px;
  }}
  .now-action .title {{
    font-size: 13px;
    color: #bfdbfe;
    margin-bottom: 4px;
    font-weight: 600;
  }}
  .now-action .desc {{
    color: #e2e8f0;
    font-size: 14px;
    line-height: 1.5;
  }}
  .start-btn {{
    margin-top: 10px;
    border: 0;
    border-radius: 10px;
    padding: 9px 12px;
    background: linear-gradient(135deg, #2563eb, #14b8a6);
    color: #fff;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }}
  .focus-timer {{
    margin-top: 8px;
    font-size: 12px;
    color: #bfdbfe;
  }}
  .streak-alert {{
    margin-top: 8px;
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 12px;
    background: rgba(245, 158, 11, 0.18);
    color: #fcd34d;
    border: 1px solid rgba(245, 158, 11, 0.35);
  }}
  .celebration {{
    margin-top: 10px;
    padding: 10px 12px;
    border-radius: 10px;
    font-size: 13px;
    color: #86efac;
    background: rgba(16, 185, 129, 0.2);
    border: 1px solid rgba(52, 211, 153, 0.4);
    animation: popIn 0.5s ease;
  }}
  @keyframes popIn {{
    from {{ transform: translateY(8px); opacity: 0; }}
    to {{ transform: translateY(0); opacity: 1; }}
  }}
  .daily-summary {{
    position: fixed;
    right: 18px;
    bottom: 18px;
    width: min(360px, calc(100vw - 24px));
    background: #111827;
    border: 1px solid #334155;
    border-radius: 12px;
    padding: 12px 14px;
    z-index: 9999;
    box-shadow: 0 16px 38px rgba(0,0,0,0.35);
  }}
  .daily-summary h4 {{ font-size: 14px; margin-bottom: 6px; color: #f8fafc; }}
  .daily-summary p {{ font-size: 12px; line-height: 1.6; color: #cbd5e1; }}
  .daily-summary button {{
    margin-top: 8px;
    border: 0;
    background: #3b82f6;
    color: #fff;
    border-radius: 8px;
    padding: 6px 10px;
    cursor: pointer;
  }}
  .guardian-risk {{
    margin-bottom: 18px;
    border: 1px solid rgba(239, 68, 68, 0.45);
    background: linear-gradient(135deg, rgba(127,29,29,0.35), rgba(30,41,59,0.85));
    border-radius: 12px;
    padding: 12px 14px;
  }}
  .guardian-risk h3 {{
    font-size: 14px;
    color: #fecaca;
    margin-bottom: 8px;
  }}
  .guardian-risk .risk-item {{
    margin-bottom: 8px;
    font-size: 12px;
    color: #f1f5f9;
    line-height: 1.55;
  }}
  .guardian-risk .risk-item:last-child {{ margin-bottom: 0; }}
  .guardian-risk .meta {{
    display: block;
    margin-top: 2px;
    color: #cbd5e1;
  }}
  .guardian-ok {{
    margin-bottom: 18px;
    border: 1px solid rgba(16, 185, 129, 0.45);
    background: linear-gradient(135deg, rgba(6,78,59,0.45), rgba(30,41,59,0.85));
    border-radius: 12px;
    padding: 10px 14px;
    color: #86efac;
    font-size: 13px;
  }}
  .xp-next {{
    margin-top: 4px;
    font-size: 11px;
    color: #93c5fd;
  }}
  .focus-overlay {{
    position: fixed;
    inset: 0;
    z-index: 10000;
    background: rgba(2, 6, 23, 0.9);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }}
  .focus-overlay .panel {{
    width: min(620px, 100%);
    border-radius: 16px;
    border: 1px solid rgba(148, 163, 184, 0.35);
    background: linear-gradient(180deg, #111827, #0f172a);
    padding: 20px;
  }}
  .focus-overlay h3 {{
    color: #f8fafc;
    font-size: 20px;
    margin-bottom: 8px;
  }}
  .focus-overlay p {{
    color: #cbd5e1;
    line-height: 1.6;
    font-size: 14px;
  }}
  .focus-overlay .clock {{
    margin: 14px 0;
    color: #93c5fd;
    font-size: 32px;
    font-weight: 700;
    letter-spacing: 1px;
  }}
  .focus-overlay .btns {{
    margin-top: 14px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .focus-overlay .btn {{
    border: 0;
    border-radius: 10px;
    padding: 9px 12px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
  }}
  .focus-overlay .btn-primary {{
    background: linear-gradient(135deg, #2563eb, #14b8a6);
    color: #fff;
  }}
  .focus-overlay .btn-success {{
    background: linear-gradient(135deg, #16a34a, #10b981);
    color: #fff;
  }}
  .focus-overlay .btn-muted {{
    background: #334155;
    color: #e2e8f0;
  }}
  .focus-overlay .hint {{
    margin-top: 8px;
    font-size: 12px;
    color: #94a3b8;
  }}
  .failure-modal {{
    position: fixed;
    inset: 0;
    z-index: 10001;
    background: rgba(2, 6, 23, 0.82);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 18px;
  }}
  .failure-modal .panel {{
    width: min(560px, 100%);
    border-radius: 14px;
    border: 1px solid #334155;
    background: #0f172a;
    padding: 16px;
  }}
  .failure-modal h4 {{
    color: #f8fafc;
    font-size: 16px;
    margin-bottom: 8px;
  }}
  .failure-modal .task {{
    color: #cbd5e1;
    font-size: 13px;
    margin-bottom: 10px;
  }}
  .failure-modal label {{
    display: block;
    color: #93c5fd;
    font-size: 12px;
    margin: 8px 0 4px 0;
  }}
  .failure-modal textarea {{
    width: 100%;
    min-height: 90px;
    resize: vertical;
    border-radius: 8px;
    border: 1px solid #334155;
    background: #111827;
    color: #e2e8f0;
    padding: 8px 10px;
    font-size: 13px;
  }}
  .failure-modal .btns {{
    margin-top: 10px;
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }}

  /* Time table */
  .time-table {{ overflow-x: auto; }}
  .time-table table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    margin-bottom: 12px;
  }}
  .time-table th {{
    background: #334155;
    padding: 8px 10px;
    text-align: left;
    font-weight: 500;
    font-size: 12px;
    color: #94a3b8;
  }}
  .time-table td {{
    padding: 8px 10px;
    border-bottom: 1px solid #1e293b;
  }}
  .time-table td.task-done {{ color: #475569; text-decoration: line-through; }}
  .time-table tr:hover {{ background: rgba(51,65,85,0.5); }}

  /* Data tables */
  .data-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    margin-bottom: 12px;
  }}
  .data-table th {{
    background: #334155;
    padding: 6px 8px;
    text-align: left;
    font-weight: 500;
    font-size: 11px;
    color: #94a3b8;
  }}
  .data-table td {{
    padding: 6px 8px;
    border-bottom: 1px solid #1e293b;
    color: #cbd5e1;
  }}
  .data-table tr:hover {{ background: rgba(51,65,85,0.5); }}
  .plan-table td:first-child {{ white-space: nowrap; }}

  /* QuickLog */
  .ql-item {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 0;
    border-bottom: 1px solid #1e293b;
    font-size: 13px;
  }}
  .ql-item:last-child {{ border-bottom: none; }}
  .ql-emoji {{ font-size: 18px; }}
  .ql-text {{ flex: 1; color: #cbd5e1; }}
  .ql-meta {{ color: #475569; font-size: 11px; white-space: nowrap; }}

  /* Timeline */
  .timeline {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    padding: 15px 0;
    position: relative;
  }}
  .timeline::before {{
    content: '';
    position: absolute;
    top: 21px;
    left: 10%;
    right: 10%;
    height: 2px;
    background: #334155;
  }}
  .tl-item {{
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: #94a3b8;
    text-align: center;
    z-index: 1;
  }}
  .tl-dot {{
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: #334155;
    border: 3px solid #475569;
  }}
  .tl-dot.now {{
    background: #3b82f6;
    border-color: #60a5fa;
    box-shadow: 0 0 12px rgba(59,130,246,0.6);
  }}
  .tl-dot.deadline {{
    background: #ef4444;
    border-color: #f87171;
    box-shadow: 0 0 12px rgba(239,68,68,0.4);
  }}
  .tl-label {{ line-height: 1.3; }}
  .tl-label small {{ color: #475569; }}

  .plan-header {{
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 10px;
    color: #60a5fa;
  }}

  /* Backward plan table */
  .backward-plan {{ margin-top: 10px; }}
  .backward-plan table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
  }}
  .backward-plan th {{
    background: #334155;
    padding: 5px 6px;
    text-align: left;
    font-weight: 500;
    color: #94a3b8;
    font-size: 10px;
  }}
  .backward-plan td {{
    padding: 5px 6px;
    border-bottom: 1px solid #1e293b;
    color: #cbd5e1;
    font-size: 11px;
  }}
  .backward-plan tr:hover {{ background: rgba(51,65,85,0.5); }}

  .empty {{
    text-align: center;
    color: #475569;
    padding: 30px;
    font-size: 13px;
  }}

  .refresh-bar {{
    text-align: center;
    color: #475569;
    font-size: 12px;
    margin-top: 24px;
    padding: 12px;
    background: #1e293b;
    border-radius: 8px;
    border: 1px solid #334155;
  }}
  .refresh-bar code {{
    background: #334155;
    padding: 2px 8px;
    border-radius: 4px;
    color: #94a3b8;
  }}

  /* Scrollbar */
  ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
  ::-webkit-scrollbar-track {{ background: #0f172a; }}
  ::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 3px; }}
  ::-webkit-scrollbar-thumb:hover {{ background: #475569; }}

  /* Fade-in animation */
  .fade-in {{
    animation: fadeIn 0.4s ease;
  }}
  @keyframes fadeIn {{
    from {{ opacity: 0; transform: translateY(8px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}

  @media (max-width: 900px) {{
    .grid {{ grid-template-columns: 1fr; }}
    .grid-3 {{ grid-template-columns: 1fr; }}
    .goals-grid {{ grid-template-columns: 1fr; }}
    .stats-bar {{ flex-direction: column; }}
  }}

  /* ─── 手机适配 (< 640px) ─── */
  @media (max-width: 640px) {{
    .container {{ padding: 12px; }}
    .header h1 {{ font-size: 24px; }}
    .card {{ padding: 14px; border-radius: 10px; }}
    .card-title {{ font-size: 14px; margin-bottom: 12px; }}

    /* 统计栏：2x2 网格 */
    .stats-bar {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }}
    .stat-item {{ padding: 12px 16px; min-width: 0; }}
    .stat-value {{ font-size: 22px; }}
    .stat-label {{ font-size: 11px; }}

    /* 时间线：纵向排列 */
    .timeline {{
      flex-direction: column;
      align-items: flex-start;
      gap: 12px;
      padding-left: 20px;
    }}
    .timeline::before {{
      top: 0; bottom: 0;
      left: 6px;
      width: 2px;
      right: auto;
    }}
    .tl-item {{
      flex-direction: row;
      align-items: center;
      gap: 12px;
      text-align: left;
    }}
    .tl-dot {{ width: 12px; height: 12px; flex-shrink: 0; }}
    .tl-label {{ font-size: 13px; }}

    /* 目标卡片 */
    .goal-card {{ padding: 14px; }}
    .goal-emoji {{ font-size: 22px; }}
    .goal-title {{ font-size: 14px; }}
    .goal-meta {{ flex-direction: column; gap: 4px; font-size: 11px; }}

    /* 任务 */
    .task-item {{
      padding: 10px 12px;
      font-size: 14px;
      border-radius: 8px;
      margin-bottom: 6px;
    }}
    .task-item .check-icon {{ font-size: 18px; }}

    /* 里程碑 */
    .ms-item {{ font-size: 13px; padding: 6px 8px; }}

    /* 今日信息 */
    .today-info {{
      flex-direction: column;
      gap: 8px;
    }}
    .today-info span {{ padding: 6px 12px; font-size: 12px; }}

    /* 表格 */
    .time-table {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    .time-table table {{ font-size: 12px; }}
    .time-table th, .time-table td {{ padding: 6px 8px; white-space: nowrap; }}
    .data-table {{ font-size: 11px; }}
    .data-table th, .data-table td {{ padding: 5px 6px; }}
    .plan-table td:first-child {{ white-space: normal; }}

    /* QuickLog */
    .ql-item {{ padding: 10px 0; font-size: 13px; }}
    .ql-meta {{ font-size: 10px; }}

    /* 进度条 */
    .progress-bar.large {{ height: 8px; }}

    /* Grid 间距缩小 */
    .grid {{ gap: 14px; }}

    /* 刷新栏 */
    .refresh-bar {{ font-size: 11px; padding: 10px; }}
  }}

  /* ─── 超小屏 (< 380px) ─── */
  @media (max-width: 380px) {{
    .container {{ padding: 8px; }}
    .header h1 {{ font-size: 20px; }}
    .stats-bar {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
    .stat-value {{ font-size: 20px; }}
    .goal-header {{ flex-wrap: wrap; }}
    .goal-title {{ font-size: 13px; }}
  }}

  /* ─── Quick Nav ─── */
  .quick-nav {{
    position: sticky;
    top: 0;
    z-index: 100;
    display: flex;
    gap: 6px;
    padding: 10px 0;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    background: #0f172a;
  }}
  .quick-nav::-webkit-scrollbar {{ display: none; }}
  .nav-pill {{
    flex-shrink: 0;
    padding: 6px 14px;
    min-height: 44px;
    display: inline-flex;
    align-items: center;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 500;
    color: #94a3b8;
    text-decoration: none;
    background: rgba(255,255,255,0.05);
    border: 1px solid transparent;
    transition: all 0.2s;
    scroll-margin-top: 60px;
    -webkit-tap-highlight-color: transparent;
  }}
  .nav-pill:hover {{
    background: rgba(255,255,255,0.1);
    color: #e2e8f0;
  }}
  .nav-pill.active {{
    background: rgba(102, 126, 234, 0.15);
    color: #667eea;
    border-color: rgba(102, 126, 234, 0.3);
  }}
  @keyframes xp-float {{
    0% {{ opacity: 1; transform: translateY(0); }}
    100% {{ opacity: 0; transform: translateY(-30px); }}
  }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🪨 Stone Plan</h1>
    <div class="subtitle"><span class="live-dot"></span>AI 驱动的生活操作系统</div>
    <div class="update-time" id="updateTime"></div>
  </div>

  <!-- Quick Nav -->
  <div class="quick-nav" id="quickNav">
    <a href="#section-timeline" class="nav-pill">📅 时间线</a>
    <a href="#section-today" class="nav-pill active">📋 今日任务</a>
    <a href="#section-goals" class="nav-pill">🎯 目标</a>
    <a href="#section-quicklog" class="nav-pill">📝 记录</a>
    <a href="#section-plan" class="nav-pill">📊 计划</a>
    <a href="#section-health" class="nav-pill">🏃 健康</a>
    <a href="#section-finance" class="nav-pill">💰 财务</a>
    <a href="#" class="nav-pill" id="backToTop" style="display:none">⬆️ 顶部</a>
  </div>

  <!-- Stats Bar -->
  <div class="stats-bar" id="statsBar"></div>
  <div id="guardianRisk"></div>

  <!-- Timeline -->
  <div class="card full-width" id="section-timeline">
    <div class="card-title"><span class="icon">📅</span> 时间线</div>
    <div id="timeline"></div>
  </div>

  <div class="grid">
    <!-- Today -->
    <div class="card" id="section-today">
      <div class="card-title"><span class="icon">📋</span> 今日任务 <span id="todayDate" style="font-size:12px;color:#94a3b8;font-weight:400;margin-left:8px;"></span></div>
      <div id="nowAction"></div>
      <div id="todaySection"></div>
    </div>

    <!-- QuickLog -->
    <div class="card" id="section-quicklog">
      <div class="card-title"><span class="icon">📝</span> 最近记录</div>
      <div id="quicklogSection"></div>
    </div>

    <!-- Goals -->
    <div class="card full-width" id="section-goals">
      <div class="card-title"><span class="icon">🎯</span> 目标总览</div>
      <div class="goals-grid" id="goalsGrid"></div>
    </div>

    <!-- Weekly Plan -->
    <div class="card full-width" id="section-plan">
      <div class="card-title"><span class="icon">📊</span> 本周计划</div>
      <div id="weeklySection"></div>
    </div>

    <!-- Health -->
    <div class="card" id="section-health">
      <div class="card-title"><span class="icon">🏃</span> 健康数据</div>
      <div id="healthSection"></div>
    </div>

    <!-- Finance -->
    <div class="card" id="section-finance">
      <div class="card-title"><span class="icon">💰</span> 财务数据</div>
      <div id="financeSection"></div>
    </div>
  </div>

  <div class="refresh-bar">
    🔄 每 10 秒自动刷新 · 运行 <code>python3 generate-dashboard.py --serve</code> 启动实时服务
  </div>
</div>

<script>
// ─── 初始数据（静态嵌入） ─────────────────────────
let dashboardData = {initial_json};
let focusTimerState = null;
let focusSession = null;
let focusSoundEnabled = localStorage.getItem('focus-sound-enabled') !== '0';
let failureDraft = null;

// ─── 渲染函数 ──────────────────────────────────────
function render(data) {{
  document.getElementById('updateTime').textContent = '最后更新: ' + data.generated_at;
  renderGuardianRisk(data.guardian_alerts || []);
  renderNowAction(data.today, data.quicklog);
  renderStats(data);
  renderTimeline(data);
  renderGoals(data.goals);
  renderToday(data.today);
  renderQuicklog(data.quicklog);
  renderWeekly(data.weekly_plan);
  renderHealth(data.health);
  renderFinance(data.finance);
  maybeShowDailySummary(data);
}}

function renderGuardianRisk(alerts) {{
  const el = document.getElementById('guardianRisk');
  if (!el) return;
  if (!alerts || alerts.length === 0) {{
    el.innerHTML = `<div class="guardian-ok">✅ 今日暂无高风险信号（Guardian 正在巡检中）</div>`;
    return;
  }}
  const html = alerts.slice(0, 3).map(a => `
    <div class="risk-item">
      • ${{a.risk}}
      <span class="meta">建议：${{a.action}}</span>
      <span class="meta">来源：${{a.file}}</span>
    </div>
  `).join('');
  el.innerHTML = `<div class="guardian-risk"><h3>🚨 今日风险提示（Guardian）</h3>${{html}}</div>`;
}}

function safeNum(v, fallback = 0) {{
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}}

function parseDeadline(raw) {{
  if (!raw || raw === '未设定') return null;
  const m = String(raw).match(/(\\d{{4}})-(\\d{{2}})-(\\d{{2}})/);
  if (!m) return null;
  return new Date(`${{m[1]}}-${{m[2]}}-${{m[3]}}T23:59:59`);
}}

function getDeadlineMeta(raw) {{
  const d = parseDeadline(raw);
  if (!d) return {{ chip: 'gray', text: '未设定' }};
  const diff = d.getTime() - Date.now();
  const abs = Math.abs(diff);
  const days = Math.floor(abs / (24 * 3600 * 1000));
  const hours = Math.floor((abs % (24 * 3600 * 1000)) / (3600 * 1000));
  const countdown = `${{days}}天${{hours}}小时`;
  if (diff < 0) return {{ chip: 'overdue', text: `已超时 ${{countdown}}` }};
  if (diff < 3 * 24 * 3600 * 1000) return {{ chip: 'red', text: `剩余 ${{countdown}}` }};
  if (diff < 7 * 24 * 3600 * 1000) return {{ chip: 'yellow', text: `剩余 ${{countdown}}` }};
  if (diff < 14 * 24 * 3600 * 1000) return {{ chip: 'blue', text: `剩余 ${{countdown}}` }};
  return {{ chip: 'gray', text: `剩余 ${{countdown}}` }};
}}

function getGoalForecast(goal) {{
  const total = safeNum(goal.milestones_total, 0);
  const done = safeNum(goal.milestones_done, 0);
  const remainingMilestones = Math.max(0, total - done);
  const deadline = goal.deadline_iso ? new Date(goal.deadline_iso + 'T23:59:59') : parseDeadline(goal.deadline);
  const start = goal.plan_start_iso ? new Date(goal.plan_start_iso + 'T00:00:00') : null;
  const now = new Date();
  const elapsedDays = start ? Math.max(1, Math.floor((now - start) / (24 * 3600 * 1000)) + 1) : 0;
  const fullSpeed = elapsedDays > 0 ? done / elapsedDays : 0;
  const weeklyPace = safeNum(goal.weekly_pace, 0);
  const trendReady = !!goal.trend_ready;
  const speed = weeklyPace > 0 ? weeklyPace : fullSpeed;
  const paceLabel = weeklyPace > 0 ? '近7天' : '全周期';
  const paceText = speed > 0 ? `${{paceLabel}} ${{speed.toFixed(2)}} 项/天` : '暂无速度数据';
  if (!deadline) {{
    return {{
      paceText,
      etaText: '预计完成: 无截止日期',
      riskText: '风险: 未知',
      riskClass: 'risk-mid',
      trendText: '趋势: 数据不足'
    }};
  }}
  if (remainingMilestones === 0) {{
    return {{
      paceText,
      etaText: '预计完成: 已完成',
      riskText: '风险: 低',
      riskClass: 'risk-low',
      trendText: '趋势: 收敛完成',
      weeklyText: `本周增量: +${{safeNum(goal.weekly_delta, 0)}} 项`
    }};
  }}
  let etaText = '预计完成: 暂无法预测';
  let riskText = '风险: 中';
  let riskClass = 'risk-mid';
  let trendText = trendReady ? '趋势: 稳步推进' : '趋势: 数据积累中';
  let weeklyText = `本周增量: ${{safeNum(goal.weekly_delta, 0) >= 0 ? '+' : ''}}${{safeNum(goal.weekly_delta, 0)}} 项/${{safeNum(goal.weekly_days, 0)}}天`;
  if (speed > 0) {{
    const daysNeed = Math.ceil(remainingMilestones / speed);
    const eta = new Date(now.getTime() + daysNeed * 24 * 3600 * 1000);
    etaText = `预计完成: ${{eta.toLocaleDateString('zh-CN')}}`;
    const delayDays = Math.ceil((eta - deadline) / (24 * 3600 * 1000));
    if (delayDays > 0) {{
      riskText = `风险: 高（预计晚 ${{delayDays}} 天）`;
      riskClass = 'risk-high';
      trendText = '趋势: 可能延期';
    }} else if (delayDays > -3) {{
      riskText = '风险: 中（接近截止）';
      riskClass = 'risk-mid';
      trendText = '趋势: 临界推进';
    }} else {{
      riskText = '风险: 低（按期可达）';
      riskClass = 'risk-low';
      trendText = '趋势: 良好';
    }}
  }} else {{
    const daysLeft = Math.ceil((deadline - now) / (24 * 3600 * 1000));
    if (daysLeft <= 3) {{
      riskText = '风险: 高（临期且未启动）';
      riskClass = 'risk-high';
      trendText = '趋势: 停滞';
    }} else {{
      trendText = '趋势: 尚未形成节奏';
    }}
  }}
  return {{ paceText, etaText, riskText, riskClass, trendText, weeklyText }};
}}

function computeXp(data) {{
  const today = data.today || {{ done: 0, total: 0 }};
  const goals = data.goals || [];
  const todayDone = safeNum(today.done);
  const todayTotal = safeNum(today.total);
  const goalDone = goals.reduce((s, g) => s + safeNum(g.milestones_done), 0);
  const goalTotal = goals.reduce((s, g) => s + safeNum(g.milestones_total), 0);
  const xp = todayDone * 15 + goalDone * 10;
  const totalXp = Math.max(1, todayTotal * 15 + goalTotal * 10);
  return {{ xp, totalXp }};
}}

function computeStreak(logs) {{
  const valid = Array.from(new Set((logs || []).map(l => l.date).filter(Boolean))).sort().reverse();
  if (!valid.length) return {{ streak: 0, hasToday: false }};
  const todayStr = new Date().toISOString().slice(0, 10);
  let streak = 1;
  for (let i = 0; i < valid.length - 1; i++) {{
    const d1 = new Date(valid[i] + 'T00:00:00');
    const d2 = new Date(valid[i + 1] + 'T00:00:00');
    const delta = Math.round((d1 - d2) / (24 * 3600 * 1000));
    if (delta === 1) streak++;
    else break;
  }}
  return {{ streak, hasToday: valid.includes(todayStr) }};
}}

function renderNowAction(today, quicklog) {{
  const el = document.getElementById('nowAction');
  if (!el) return;
  if (!today || !today.tasks || !today.tasks.length) {{
    el.innerHTML = `<div class="now-action"><div class="title">🧭 现在该做什么</div><div class="desc">先创建今天的任务清单，给自己一个明确的开场动作。</div></div>`;
    return;
  }}
  const undone = today.tasks.filter(t => !t.done);
  const hour = new Date().getHours();
  const greeting = hour < 12 ? '上午行动' : (hour < 18 ? '下午推进' : '今晚收口');
  const first = undone[0];
  const firstIndex = today.tasks.findIndex(t => !t.done);
  const streakMeta = computeStreak(quicklog);
  let warning = '';
  if (hour >= 21 && undone.length > 0) {{
    warning = `<div class="streak-alert">⚠️ 今天还有 ${{undone.length}} 项未完成，建议先做第一项，避免连击中断。</div>`;
  }} else if (!streakMeta.hasToday && streakMeta.streak > 0) {{
    warning = `<div class="streak-alert">🔥 当前连击 ${{streakMeta.streak}} 天，今天记得至少完成一项并记录。</div>`;
  }}
  const desc = first ? `下一步就做：${{first.text}}` : '今日任务已完成，建议做 1 条 QuickLog 记录巩固成果。';
  const button = first ? `<button class="start-btn" onclick="startFocusSprint(${{firstIndex}})">开始第一项（25分钟）</button><div id="focusTimer" class="focus-timer"></div>` : '';
  el.innerHTML = `<div class="now-action"><div class="title">🧭 ${{greeting}} · 现在该做什么</div><div class="desc">${{desc}}</div>${{button}}${{warning}}</div>`;
}}

function renderStats(data) {{
  const goals = data.goals || [];
  const today = data.today;
  const goalCount = goals.length;
  const totalTasks = today ? today.total : 0;
  const doneTasks = today ? today.done : 0;
  const logs = (data.quicklog || []).length;
  const {{ xp, totalXp }} = computeXp(data);
  const streakMeta = computeStreak(data.quicklog || []);
  const nextLevelXp = Math.ceil((xp + 1) / 100) * 100;
  const toNext = Math.max(0, nextLevelXp - xp);

  document.getElementById('statsBar').innerHTML = `
    <div class="stat-item">
      <div class="stat-value">${{goalCount}}</div>
      <div class="stat-label">🎯 目标数</div>
    </div>
    <div class="stat-item">
      <div class="stat-value">${{doneTasks}}/${{totalTasks}}</div>
      <div class="stat-label">📋 今日进度</div>
    </div>
    <div class="stat-item">
      <div class="stat-value xp">${{xp}}/${{totalXp}}</div>
      <div class="stat-label">⚡ XP / 总XP</div>
      <div class="xp-next">距离下一级还差 ${{toNext}} XP</div>
    </div>
    <div class="stat-item">
      <div class="stat-value streak">${{streakMeta.streak}}</div>
      <div class="stat-label">🔥 活跃连击数</div>
    </div>
    <div class="stat-item">
      <div class="stat-value">${{logs}}</div>
      <div class="stat-label">📝 记录数</div>
    </div>
  `;
}}

function startFocusSprint(taskIndex) {{
  const timerEl = document.getElementById('focusTimer');
  if (!timerEl) return;
  const today = dashboardData.today || {{}};
  const task = (today.tasks || [])[taskIndex];
  if (!task) return;
  if (focusTimerState && focusTimerState.intervalId) clearInterval(focusTimerState.intervalId);
  const end = Date.now() + 25 * 60 * 1000;
  focusSession = {{ taskIndex: taskIndex, taskText: task.text, endAt: end }};
  openFocusOverlay();
  const tick = () => {{
    const left = Math.max(0, end - Date.now());
    const mm = String(Math.floor(left / 60000)).padStart(2, '0');
    const ss = String(Math.floor((left % 60000) / 1000)).padStart(2, '0');
    timerEl.textContent = left > 0 ? `⏳ 专注进行中：${{mm}}:${{ss}}` : '✅ 25分钟完成，去勾选这一项吧！';
    updateFocusOverlayClock(mm, ss, left <= 0);
    if (left <= 0 && focusTimerState && focusTimerState.intervalId) {{
      clearInterval(focusTimerState.intervalId);
      focusTimerState.intervalId = null;
    }}
  }};
  tick();
  focusTimerState = {{
    intervalId: setInterval(tick, 1000)
  }};
}}

function openFocusOverlay() {{
  if (!focusSession) return;
  closeFocusOverlay();
  const overlay = document.createElement('div');
  overlay.id = 'focusOverlay';
  overlay.className = 'focus-overlay';
  overlay.innerHTML = `
    <div class="panel">
      <h3>🎯 专注模式</h3>
      <p>当前只做这一件事：</p>
      <p><strong>${{focusSession.taskText}}</strong></p>
      <div class="clock" id="focusOverlayClock">25:00</div>
      <div class="btns">
        <button class="btn btn-success" id="focusDoneBtn" style="display:none;">我已完成，立即打勾</button>
        <button class="btn btn-primary" id="focusContinueBtn">继续专注</button>
        <button class="btn btn-muted" id="focusSoundBtn"></button>
        <button class="btn btn-muted" id="focusCloseBtn">稍后继续</button>
      </div>
      <div class="hint">按 Esc 可退出专注模式</div>
    </div>
  `;
  document.body.appendChild(overlay);
  const doneBtn = document.getElementById('focusDoneBtn');
  const contBtn = document.getElementById('focusContinueBtn');
  const soundBtn = document.getElementById('focusSoundBtn');
  const closeBtn = document.getElementById('focusCloseBtn');
  if (soundBtn) {{
    soundBtn.textContent = focusSoundEnabled ? '🔔 提示音：开' : '🔕 提示音：关';
    soundBtn.addEventListener('click', toggleFocusSound);
  }}
  if (doneBtn) doneBtn.addEventListener('click', completeFocusTask);
  if (contBtn) contBtn.addEventListener('click', () => null);
  if (closeBtn) closeBtn.addEventListener('click', closeFocusOverlay);
  document.addEventListener('keydown', handleFocusOverlayKeydown);
}}

function updateFocusOverlayClock(mm, ss, finished) {{
  const clock = document.getElementById('focusOverlayClock');
  if (!clock) return;
  clock.textContent = `${{mm}}:${{ss}}`;
  const doneBtn = document.getElementById('focusDoneBtn');
  const continueBtn = document.getElementById('focusContinueBtn');
  if (finished) {{
    if (doneBtn) doneBtn.style.display = '';
    if (continueBtn) continueBtn.textContent = '已到时，查看任务';
    if (!focusSession || !focusSession.rang) {{
      playFocusDoneSound();
      if (focusSession) focusSession.rang = true;
    }}
  }}
}}

function closeFocusOverlay() {{
  const el = document.getElementById('focusOverlay');
  if (el) el.remove();
  document.removeEventListener('keydown', handleFocusOverlayKeydown);
}}

function handleFocusOverlayKeydown(e) {{
  if (e.key === 'Escape') {{
    closeFocusOverlay();
  }}
}}

function toggleFocusSound() {{
  focusSoundEnabled = !focusSoundEnabled;
  localStorage.setItem('focus-sound-enabled', focusSoundEnabled ? '1' : '0');
  const soundBtn = document.getElementById('focusSoundBtn');
  if (soundBtn) {{
    soundBtn.textContent = focusSoundEnabled ? '🔔 提示音：开' : '🔕 提示音：关';
  }}
}}

function playFocusDoneSound() {{
  if (!focusSoundEnabled) return;
  try {{
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const now = ctx.currentTime;
    const play = (freq, start, duration) => {{
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.1, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(start);
      osc.stop(start + duration);
    }};
    play(880, now, 0.16);
    play(1174, now + 0.18, 0.16);
    play(1568, now + 0.36, 0.2);
  }} catch (e) {{
    // Ignore if audio context is unavailable.
  }}
}}

function completeFocusTask() {{
  if (!focusSession) return;
  const taskIndex = focusSession.taskIndex;
  const taskItems = document.querySelectorAll('.task-item');
  if (taskItems[taskIndex]) {{
    const el = taskItems[taskIndex];
    el.querySelector('.check-icon').textContent = '✅';
    el.classList.add('done');
    showXpFloat(el, '+10 XP');
  }}

  const queue = JSON.parse(localStorage.getItem('stone_sync_queue') || '[]');
  queue.push({{
    id: Date.now().toString(36) + Math.random().toString(36).substr(2, 5),
    type: 'toggle-task',
    index: taskIndex,
    done: true,
    timestamp: new Date().toISOString()
  }});
  localStorage.setItem('stone_sync_queue', JSON.stringify(queue));

  closeFocusOverlay();
  updateTodayProgressUI(1);

  fetch('/api/toggle-task', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ index: taskIndex }})
  }}).catch(() => {{}});
}}

function openFailureReview(taskIndex) {{
  const today = dashboardData.today || {{}};
  const t = (today.tasks || [])[taskIndex];
  if (!t) return;
  closeFailureReviewModal();
  failureDraft = {{ taskIndex, task: t.text, slot: t.slot || '' }};
  const modal = document.createElement('div');
  modal.id = 'failureModal';
  modal.className = 'failure-modal';
  modal.innerHTML = `
    <div class="panel">
      <h4>🧩 失败复盘</h4>
      <div class="task">任务：${{t.text}} ${{t.slot ? `（${{t.slot}}）` : ''}}</div>
      <label>失败原因（发生了什么）</label>
      <textarea id="failureReason" placeholder="例如：被临时事务打断，没有在预定时间窗启动。"></textarea>
      <label>改进动作（下一次怎么避免）</label>
      <textarea id="failureAction" placeholder="例如：提前 10 分钟清空通知；把任务拆成 20 分钟可执行子任务。"></textarea>
      <div class="btns">
        <button class="btn btn-muted" id="failureCancelBtn">取消</button>
        <button class="btn btn-primary" id="failureSaveBtn">保存复盘</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  const cancelBtn = document.getElementById('failureCancelBtn');
  const saveBtn = document.getElementById('failureSaveBtn');
  if (cancelBtn) cancelBtn.addEventListener('click', closeFailureReviewModal);
  if (saveBtn) saveBtn.addEventListener('click', submitFailureReview);
}}

function closeFailureReviewModal() {{
  const modal = document.getElementById('failureModal');
  if (modal) modal.remove();
}}

function submitFailureReview() {{
  if (!failureDraft) return;
  const today = dashboardData.today || {{}};
  const reason = (document.getElementById('failureReason') || {{ value: '' }}).value.trim();
  const action = (document.getElementById('failureAction') || {{ value: '' }}).value.trim();
  if (!reason) {{
    window.alert('请先填写失败原因。');
    return;
  }}

  const queue = JSON.parse(localStorage.getItem('stone_sync_queue') || '[]');
  queue.push({{
    id: Date.now().toString(36) + Math.random().toString(36).substr(2, 5),
    type: 'failure-review',
    task_text: failureDraft.task,
    reason: reason,
    learnings: action,
    timestamp: new Date().toISOString()
  }});
  localStorage.setItem('stone_sync_queue', JSON.stringify(queue));

  closeFailureReviewModal();
  window.alert('失败复盘已记录到 reviews 和 quicklog。');

  fetch('/api/save-failure-review', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      date: today.date,
      task: failureDraft.task,
      slot: failureDraft.slot,
      reason: reason,
      action: action
    }})
  }}).catch(() => {{}});
}}

function renderTimeline(data) {{
  const goals = (data.goals || []).filter(g => g.deadline && g.deadline !== '未设定');
  // Sort by remaining days (nearest deadline first)
  goals.sort((a, b) => parseDeadline(a.deadline) - parseDeadline(b.deadline));
  let items = '<div class="tl-item"><div class="tl-dot now"></div><div class="tl-label">今天<br><small>' +
    new Date().toLocaleDateString('zh-CN') + '</small></div></div>';
  for (const g of goals) {{
    const meta = getDeadlineMeta(g.deadline);
    items += `<div class="tl-item"><div class="tl-dot deadline"></div><div class="tl-label">${{g.emoji}} ${{g.title}}<br><small>${{g.deadline}} · ${{meta.text}}</small></div></div>`;
  }}
  document.getElementById('timeline').innerHTML = '<div class="timeline">' + items + '</div>';
}}

function renderGoals(goals) {{
  if (!goals || goals.length === 0) {{
    document.getElementById('goalsGrid').innerHTML = '<p class="empty">暂无目标</p>';
    return;
  }}
  let html = '';
  for (const g of goals) {{
    const pct = g.milestone_pct || 0;
    const fillClass = pct >= 80 ? 'green' : pct >= 40 ? '' : 'red';

    let msHtml = '';
    for (let ti = 0; ti < (g.tasks || []).slice(0, 10).length; ti++) {{
      const t = (g.tasks || [])[ti];
      const icon = t.done ? '✅' : '⬜';
      msHtml += `<div class="ms-item ${{t.done ? 'done' : ''}}" onclick="toggleMilestone('${{g.file}}', ${{ti}}, this)" title="点击切换状态">${{icon}} ${{t.text}}</div>`;
    }}
    if ((g.tasks || []).length > 10) {{
      msHtml += `<div class="ms-item" style="color:#475569;cursor:default">...还有 ${{g.tasks.length - 10}} 项</div>`;
    }}

    // Backward plan table
    let bpHtml = '';
    if (g.backward_table) {{
      bpHtml = '<div class="backward-plan"><table><thead><tr>';
      for (const h of g.backward_table.headers) bpHtml += `<th>${{h}}</th>`;
      bpHtml += '</tr></thead><tbody>';
      for (const row of g.backward_table.rows) {{
        bpHtml += '<tr>';
        for (const cell of row) bpHtml += `<td>${{cell}}</td>`;
        bpHtml += '</tr>';
      }}
      bpHtml += '</tbody></table></div>';
    }}

    // Deadline display
    const tagsHtml = (g.tags || []).map(t => `<span class="tag">${{t}}</span>`).join('');
    const deadlineMeta = getDeadlineMeta(g.deadline);
    const forecast = getGoalForecast(g);

    html += `
    <div class="goal-card fade-in" style="border-left-color:${{g.status_color}}">
      <div class="goal-header">
        <span class="goal-emoji">${{g.emoji}}</span>
        <span class="goal-title">${{g.title}}</span>
        <span class="goal-status" style="background:${{g.status_color}}">${{g.status}}</span>
      </div>
      <div class="goal-meta">
        <span>📅 ${{g.deadline}}</span>
        <span class="deadline-chip ${{deadlineMeta.chip}}">⏰ ${{deadlineMeta.text}}</span>
        <span>📊 ${{g.progress}}</span>
      </div>
      <div class="progress-bar">
        <div class="progress-fill ${{fillClass}}" style="width:${{pct}}%"></div>
      </div>
      <div class="goal-forecast">
        <div class="chip">📈 ${{forecast.trendText}}</div>
        <div class="chip ${{forecast.riskClass}}">⚠️ ${{forecast.riskText}}</div>
        <div class="chip">⏱️ ${{forecast.paceText}}</div>
        <div class="chip">🗓️ ${{forecast.etaText}}</div>
        <div class="chip" style="grid-column:1 / -1">📊 ${{forecast.weeklyText}}</div>
      </div>
      <div class="milestones">${{msHtml}}</div>
      ${{bpHtml}}
      <div class="goal-tags">${{tagsHtml}}</div>
    </div>`;
  }}
  document.getElementById('goalsGrid').innerHTML = html;
}}

function renderToday(today) {{
  const el = document.getElementById('todaySection');
  if (!today) {{
    el.innerHTML = '<p class="empty">暂无今日任务</p>';
    return;
  }}

  let html = '';

  // Set date in title
  const dateEl = document.getElementById('todayDate');
  if (dateEl) dateEl.textContent = today.date;

  // Time table
  if (today.time_table) {{
    html += '<div class="time-table"><table><thead><tr>';
    for (const h of today.time_table.headers) html += `<th>${{h}}</th>`;
    html += '</tr></thead><tbody>';
    for (const row of today.time_table.rows) {{
      html += '<tr>';
      for (const cell of row) {{
        const cls = cell.includes('✅') ? ' class="task-done"' : '';
        html += `<td${{cls}}>${{cell}}</td>`;
      }}
      html += '</tr>';
    }}
    html += '</tbody></table></div>';
  }}

  // Info bar
  html += `<div class="today-info">
    <span>🔋 ${{today.energy}}</span>
    <span>😴 ${{today.sleep_time}} ~ ${{today.wake_time}} (${{today.sleep_hours}}h)</span>
  </div>`;

  // Progress
  html += `<div class="progress-section">
    <div class="progress-label" id="todayProgressLabel">今日进度: ${{today.done}}/${{today.total}} (${{today.pct}}%)</div>
    <div class="progress-bar large">
      <div id="todayProgressFill" class="progress-fill ${{today.pct >= 80 ? 'green' : today.pct >= 40 ? '' : 'red'}}" style="width:${{today.pct}}%"></div>
    </div>
  </div>`;

  // Task list
  html += '<div class="task-list">';
  for (let ti = 0; ti < today.tasks.length; ti++) {{
    const t = today.tasks[ti];
    const icon = t.done ? '✅' : '⬜';
    const deadlineChip = t.slot ? `<span class="task-deadline">⏰ ${{t.slot}}</span>` : '';
    html += `<div class="task-item ${{t.done ? 'done' : ''}}" onclick="toggleTask(${{ti}}, this)" title="点击切换完成状态"><span class="check-icon">${{icon}}</span><span class="task-main">${{t.text}}</span>${{deadlineChip}}<button class="fail-btn" onclick="event.stopPropagation();openFailureReview(${{ti}})">失败复盘</button></div>`;
  }}
  html += '</div>';
  if (today.total > 0 && today.done === today.total) {{
    html += '<div class="celebration">🎉 今日任务全部完成！继续写一条复盘，连击更稳。</div>';
  }}

  el.innerHTML = html;
}}

function renderQuicklog(logs) {{
  const el = document.getElementById('quicklogSection');
  if (!logs || logs.length === 0) {{
    el.innerHTML = '<p class="empty">暂无记录</p>';
    return;
  }}
  const emojiMap = {{
    exercise: '🏃', expense: '💰', mood: '😊', task: '✅',
    note: '📝', weight: '⚖️', sleep: '😴', study: '📖'
  }};
  let html = '';
  for (const log of logs) {{
    const e = emojiMap[log.type] || '📌';
    html += `<div class="ql-item">
      <span class="ql-emoji">${{e}}</span>
      <span class="ql-text">${{log.record || log.type}}</span>
      <span class="ql-meta">${{log.date}} ${{log.time}}</span>
    </div>`;
  }}
  el.innerHTML = html;
}}

function renderWeekly(plan) {{
  const el = document.getElementById('weeklySection');
  if (!plan) {{
    el.innerHTML = '<p class="empty">暂无周计划</p>';
    return;
  }}
  let html = `<div class="plan-header">📋 ${{plan.period}}</div>`;
  for (const t of plan.tables) {{
    html += '<table class="data-table plan-table"><thead><tr>';
    for (const h of t.headers) html += `<th>${{h}}</th>`;
    html += '</tr></thead><tbody>';
    for (const row of t.rows) {{
      html += '<tr>';
      for (const cell of row) html += `<td>${{cell}}</td>`;
      html += '</tr>';
    }}
    html += '</tbody></table>';
  }}
  el.innerHTML = html;
}}

function renderTableHtml(tables) {{
  let html = '';
  for (const t of tables) {{
    html += '<table class="data-table"><thead><tr>';
    for (const h of t.headers) html += `<th>${{h}}</th>`;
    html += '</tr></thead><tbody>';
    for (const row of t.rows) {{
      html += '<tr>';
      for (const cell of row) html += `<td>${{cell}}</td>`;
      html += '</tr>';
    }}
    html += '</tbody></table>';
  }}
  return html;
}}

function renderHealth(health) {{
  const el = document.getElementById('healthSection');
  if (!health || Object.keys(health).length === 0) {{
    el.innerHTML = '<p class="empty">暂无数据</p>';
    return;
  }}
  let html = '';
  for (const [name, tables] of Object.entries(health)) {{
    if (tables && tables.length > 0) {{
      const labels = {{ weight: '⚖️ 体重', exercise: '🏃 运动', sleep: '😴 睡眠', diet: '🥗 饮食' }};
      html += `<div style="margin-bottom:12px"><strong style="color:#94a3b8;font-size:12px">${{labels[name] || name}}</strong></div>`;
      html += renderTableHtml(tables);
    }}
  }}
  el.innerHTML = html || '<p class="empty">暂无数据</p>';
}}

function renderFinance(finance) {{
  const el = document.getElementById('financeSection');
  if (!finance || Object.keys(finance).length === 0) {{
    el.innerHTML = '<p class="empty">暂无数据</p>';
    return;
  }}
  let html = '';
  for (const [name, tables] of Object.entries(finance)) {{
    if (tables && tables.length > 0) {{
      const labels = {{ expense: '💸 支出', budget: '📋 预算', income: '💰 收入' }};
      html += `<div style="margin-bottom:12px"><strong style="color:#94a3b8;font-size:12px">${{labels[name] || name}}</strong></div>`;
      html += renderTableHtml(tables);
    }}
  }}
  el.innerHTML = html || '<p class="empty">暂无数据</p>';
}}

// ─── 交互：点击切换任务/里程碑状态 ──────────────────
function showXpFloat(el, text) {{
  const float = document.createElement('div');
  float.textContent = text;
  float.style.cssText = `
    position: absolute;
    top: 50%;
    right: 10px;
    color: #667eea;
    font-weight: 700;
    font-size: 14px;
    pointer-events: none;
    animation: xp-float 1s ease-out forwards;
    z-index: 100;
  `;
  el.style.position = 'relative';
  el.appendChild(float);
  setTimeout(() => float.remove(), 1000);
}}

function toggleTask(index, el) {{
  if (el.classList.contains('syncing')) return;

  // 1. 乐观更新 UI
  const wasDone = el.classList.contains('done');
  el.querySelector('.check-icon').textContent = wasDone ? '⬜' : '✅';
  el.classList.toggle('done', !wasDone);

  // 2. 添加 XP 飘字动画
  if (!wasDone) {{
    showXpFloat(el, '+10 XP');
  }}

  // 3. 写入 localStorage 待同步队列
  const queue = JSON.parse(localStorage.getItem('stone_sync_queue') || '[]');
  queue.push({{
    id: Date.now().toString(36) + Math.random().toString(36).substr(2, 5),
    type: 'toggle-task',
    index: index,
    done: !wasDone,
    timestamp: new Date().toISOString()
  }});
  localStorage.setItem('stone_sync_queue', JSON.stringify(queue));

  // 4. 更新进度 UI
  updateTodayProgressUI(wasDone ? -1 : 1);

  // 5. 检查是否全部完成
  if (!wasDone) {{
    const pending = document.querySelectorAll('.task-item:not(.done)');
    if (pending.length === 0) {{
      celebrateCompletion();
    }}
  }}

  // 6. 1秒后尝试 POST 同步（本地访问时生效，iframe 中会静默失败）
  fetch('/api/toggle-task', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ index: index }})
  }}).catch(() => {{}}); // 静默忽略错误
}}

function updateTodayProgressUI(deltaDone) {{
  const label = document.getElementById('todayProgressLabel');
  const fill = document.getElementById('todayProgressFill');
  if (!label || !fill) return;
  const m = label.textContent.match(/(\\d+)\\/(\\d+)/);
  if (!m) return;
  const total = safeNum(m[2], 0);
  const currentDone = safeNum(m[1], 0);
  const nextDone = Math.max(0, Math.min(total, currentDone + deltaDone));
  const pct = total > 0 ? Math.round(nextDone / total * 100) : 0;
  label.textContent = `今日进度: ${{nextDone}}/${{total}} (${{pct}}%)`;
  fill.style.width = `${{pct}}%`;
  fill.classList.toggle('green', pct >= 80);
  fill.classList.toggle('red', pct < 40);
  fill.classList.add('pulse');
  setTimeout(() => fill.classList.remove('pulse'), 850);
}}

function toggleMilestone(goalFile, index, el) {{
  const wasDone = el.classList.contains('done');
  const icon = wasDone ? '⬜' : '✅';
  el.firstChild.textContent = icon + ' ';
  el.classList.toggle('done', !wasDone);

  const queue = JSON.parse(localStorage.getItem('stone_sync_queue') || '[]');
  queue.push({{
    id: Date.now().toString(36) + Math.random().toString(36).substr(2, 5),
    type: 'toggle-milestone',
    goal_file: goalFile,
    index: index,
    done: !wasDone,
    timestamp: new Date().toISOString()
  }});
  localStorage.setItem('stone_sync_queue', JSON.stringify(queue));

  fetch('/api/toggle-milestone', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ goal_file: goalFile, index: index }})
  }}).catch(() => {{}});
}}

function maybeShowDailySummary(data) {{
  const today = data.today;
  if (!today) return;
  const now = new Date();
  if (now.getHours() < 23) return;
  const key = `daily-summary-${{today.date}}`;
  if (localStorage.getItem(key)) return;
  const {{ xp, totalXp }} = computeXp(data);
  const streakMeta = computeStreak(data.quicklog || []);
  const summary = document.createElement('div');
  summary.className = 'daily-summary';
  summary.innerHTML = `
    <h4>🌙 今日总结</h4>
    <p>完成任务：${{today.done}}/${{today.total}}<br>XP：${{xp}}/${{totalXp}}<br>连击：${{streakMeta.streak}} 天<br>明日建议：先完成今天未完成项中最重要的一项。</p>
    <button id="closeDailySummary">知道了</button>
  `;
  document.body.appendChild(summary);
  const closeBtn = document.getElementById('closeDailySummary');
  if (closeBtn) {{
    closeBtn.addEventListener('click', () => {{
      localStorage.setItem(key, '1');
      const undone = (today.tasks || []).filter(t => !t.done);
      fetch('/api/save-daily-summary', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          date: today.date,
          done: today.done,
          total: today.total,
          xp: xp,
          total_xp: totalXp,
          streak: streakMeta.streak,
          next_action: undone.length ? `优先完成：${{undone[0].text}}` : '保持节奏并安排明日第一项任务'
        }})
      }}).catch(() => null);
      summary.remove();
    }});
  }}
}}

// ─── 动态刷新 ──────────────────────────────────────
function applyPendingOps(data) {{
  // 把 localStorage 待同步队列中的操作叠加到服务器数据上
  // 这样即使 POST 失败，乐观状态也不会被 render 覆盖
  const queue = JSON.parse(localStorage.getItem('stone_sync_queue') || '[]');
  if (queue.length === 0) return data;

  const taskOps = queue.filter(q => q.type === 'toggle-task');
  const milestoneOps = queue.filter(q => q.type === 'toggle-milestone');

  // 应用任务操作
  if (taskOps.length > 0 && data.today && data.today.tasks) {{
    for (const op of taskOps) {{
      if (data.today.tasks[op.index]) {{
        data.today.tasks[op.index].done = op.done;
      }}
    }}
    // 重新计算 done/total/pct
    data.today.done = data.today.tasks.filter(t => t.done).length;
    data.today.total = data.today.tasks.length;
    data.today.pct = data.today.total > 0 ? Math.round(data.today.done / data.today.total * 100) : 0;
  }}

  // 应用里程碑操作
  if (milestoneOps.length > 0 && data.goals) {{
    for (const op of milestoneOps) {{
      for (const g of data.goals) {{
        if (g.file === op.goal_file && g.tasks && g.tasks[op.index]) {{
          g.tasks[op.index].done = op.done;
        }}
      }}
    }}
  }}

  return data;
}}

function cleanSyncedQueue(serverData) {{
  const queue = JSON.parse(localStorage.getItem('stone_sync_queue') || '[]');
  if (queue.length === 0) return;

  const remaining = [];
  for (const item of queue) {{
    let synced = false;
    if (item.type === 'toggle-task' && serverData.today && serverData.today.tasks) {{
      const task = serverData.today.tasks[item.index];
      if (task && task.done === item.done) synced = true;
    }}
    if (item.type === 'toggle-milestone' && serverData.goals) {{
      for (const g of serverData.goals) {{
        if (g.file === item.goal_file && g.tasks && g.tasks[item.index]) {{
          if (g.tasks[item.index].done === item.done) synced = true;
        }}
      }}
    }}
    if (!synced) remaining.push(item);
  }}

  if (remaining.length !== queue.length) {{
    localStorage.setItem('stone_sync_queue', JSON.stringify(remaining));
  }}
}}

function fetchAndUpdate() {{
  // 收集待同步队列
  const queue = JSON.parse(localStorage.getItem('stone_sync_queue') || '[]');
  let url = '/api/data';
  if (queue.length > 0) {{
    // 通过 GET 参数把队列发给服务器，服务器会自动执行并改 md
    url += '?sync=' + encodeURIComponent(JSON.stringify(queue));
  }}

  fetch(url)
    .then(r => r.json())
    .then(data => {{
      // 服务器已执行同步，清理本地队列
      if (queue.length > 0) {{
        localStorage.setItem('stone_sync_queue', '[]');
      }}

      // 叠加可能残留的本地操作（正常情况下应该为空了）
      data = applyPendingOps(data);
      dashboardData = data;
      render(data);

      // 清理已同步的队列项
      cleanSyncedQueue(data);
    }})
    .catch(err => {{
      console.log('Fetch failed, using embedded data:', err.message);
    }});
}}

// Initial render
render(dashboardData);

// Auto-refresh every 10 seconds
// Cache buster
console.log('Stone Plan Dashboard v2.0.1 - ' + new Date().toISOString());

setInterval(fetchAndUpdate, 10000);

// Scroll spy for nav highlighting
const sections = ['timeline','today','goals','quicklog','plan','health','finance'];
const navPills = document.querySelectorAll('.nav-pill');

function updateActiveNav() {{
  let current = '';
  const scrolled = window.scrollY > 300;
  document.getElementById('backToTop').style.display = scrolled ? '' : 'none';
  for (const id of sections) {{
    const el = document.getElementById('section-' + id);
    if (el && el.getBoundingClientRect().top <= 120) {{
      current = id;
    }}
  }}
  navPills.forEach(pill => {{
    pill.classList.toggle('active', pill.getAttribute('href') === '#section-' + current);
  }});
}}
window.addEventListener('scroll', updateActiveNav, {{ passive: true }});

// Smooth scroll for nav pills
navPills.forEach(pill => {{
  pill.addEventListener('click', e => {{
    e.preventDefault();
    const href = pill.getAttribute('href');
    if (!href || href === '#') {{
      window.scrollTo({{ top: 0, behavior: 'smooth' }});
      return;
    }}
    const target = document.querySelector(href);
    if (target) {{
      target.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }}
  }});
}});
</script>
</body>
</html>'''

    with open(OUTPUT_HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    console_print(f"✅ Dashboard generated: {OUTPUT_HTML}")
    console_print(f"   Goals: {len(data['goals'])}")
    if data['today']:
        console_print(f"   Today tasks: {data['today']['done']}/{data['today']['total']}")
    else:
        console_print("   Today tasks: N/A")
    console_print(f"   QuickLog entries: {len(data['quicklog'])}")

# ─── md 文件修改函数 ────────────────────────────────────────────

def toggle_today_task(task_index):
    """切换今日任务状态（在时间轴表格中 ⬜↔✅）"""
    today = datetime.now().strftime('%Y-%m-%d')
    today_path = os.path.join(BASE_DIR, f"tasks/daily/{today[:4]}/{today[5:7]}/{today}.md")
    if not os.path.exists(today_path):
        return {'ok': False, 'error': '今日任务文件不存在'}

    with open(today_path, 'r', encoding='utf-8') as f:
        content = f.read()

    lines = content.split('\n')
    # Find the time table section
    in_time_table = False
    table_start = -1
    data_rows = []
    status_col = -1
    task_col = -1

    for i, line in enumerate(lines):
        if '| 时间' in line and '任务' in line and '状态' in line:
            in_time_table = True
            table_start = i
            headers = [c.strip() for c in line.split('|')[1:-1]]
            for j, h in enumerate(headers):
                if '状态' in h:
                    status_col = j
                if '任务' in h:
                    task_col = j
            continue
        if in_time_table:
            if line.strip().startswith('|-') or line.strip().startswith('| --'):
                continue  # separator
            if '|' in line and line.strip().startswith('|'):
                data_rows.append(i)
            else:
                in_time_table = False  # end of table

    if task_index >= len(data_rows):
        return {'ok': False, 'error': f'任务索引 {task_index} 超出范围（共 {len(data_rows)} 个）'}

    target_line = data_rows[task_index]
    cells = lines[target_line].split('|')
    if status_col + 1 < len(cells):
        old_status = cells[status_col + 1].strip()
        if '✅' in old_status:
            cells[status_col + 1] = ' ⬜ '
            new_status = False
        else:
            cells[status_col + 1] = ' ✅ '
            new_status = True
        lines[target_line] = '|'.join(cells)

        with open(today_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        return {'ok': True, 'done': new_status, 'index': task_index}
    return {'ok': False, 'error': '找不到状态列'}

def toggle_goal_milestone(goal_file, milestone_index):
    """切换目标里程碑状态（- [ ] ↔ - [x]）"""
    goal_path = os.path.join(BASE_DIR, f"goals/active/{goal_file}")
    if not os.path.exists(goal_path):
        return {'ok': False, 'error': f'目标文件 {goal_file} 不存在'}

    with open(goal_path, 'r', encoding='utf-8') as f:
        content = f.read()

    lines = content.split('\n')
    checklist_indices = []
    for i, line in enumerate(lines):
        if re.match(r'^\s*[-*]\s*\[[ xX]\]', line):
            checklist_indices.append(i)

    if milestone_index >= len(checklist_indices):
        return {'ok': False, 'error': f'里程碑索引 {milestone_index} 超出范围'}

    target = checklist_indices[milestone_index]
    old_line = lines[target]
    if '[x]' in old_line or '[X]' in old_line:
        lines[target] = re.sub(r'\[[xX]\]', '[ ]', old_line)
        new_status = False
    else:
        lines[target] = re.sub(r'\[ \]', '[x]', old_line)
        new_status = True

    with open(goal_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    return {'ok': True, 'done': new_status, 'index': milestone_index}

# ─── 文件管理函数 ────────────────────────────────────────────────

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXTENSIONS = {
    '.md', '.txt', '.csv', '.json', '.pdf',
    '.png', '.jpg', '.jpeg', '.gif', '.webp',
    '.doc', '.docx', '.xls', '.xlsx',
}

def ensure_upload_dir():
    os.makedirs(UPLOAD_DIR, exist_ok=True)

def list_uploaded_files():
    """列出所有已上传文件"""
    ensure_upload_dir()
    files = []
    for f in sorted(os.listdir(UPLOAD_DIR), reverse=True):
        path = os.path.join(UPLOAD_DIR, f)
        if os.path.isfile(path):
            stat = os.stat(path)
            ext = os.path.splitext(f)[1].lower()
            files.append({
                'name': f,
                'size': stat.st_size,
                'size_human': _human_size(stat.st_size),
                'ext': ext,
                'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
            })
    return files

def upload_file(filename, file_data):
    """保存上传文件，返回文件信息"""
    ensure_upload_dir()
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return {'ok': False, 'error': f'不支持的文件格式: {ext}，支持: {", ".join(sorted(ALLOWED_EXTENSIONS))}'}
    # 清理文件名
    safe_name = re.sub(r'[^\w\.\-\u4e00-\u9fff]', '_', filename)
    # 避免重名
    dest = os.path.join(UPLOAD_DIR, safe_name)
    if os.path.exists(dest):
        name, ext = os.path.splitext(safe_name)
        safe_name = f"{name}_{uuid.uuid4().hex[:6]}{ext}"
        dest = os.path.join(UPLOAD_DIR, safe_name)
    with open(dest, 'wb') as f:
        if isinstance(file_data, bytes):
            f.write(file_data)
        else:
            f.write(file_data.encode('utf-8'))
    return {'ok': True, 'file': safe_name, 'size': os.path.getsize(dest)}

def delete_file(filename):
    """删除上传文件"""
    filepath = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return {'ok': False, 'error': '文件不存在'}
    os.remove(filepath)
    return {'ok': True, 'file': filename}

def _human_size(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"

def scan_uploads():
    """扫描上传文件，供 dashboard 数据使用"""
    return list_uploaded_files()

# ─── 同步队列函数 ─────────────────────────────────────────────

def get_sync_queue():
    """读取待同步队列"""
    if os.path.exists(SYNC_QUEUE_FILE):
        with open(SYNC_QUEUE_FILE, 'r') as f:
            return json.load(f)
    return []

def ack_sync_item(queue_id):
    """确认已同步，从队列中移除"""
    queue = get_sync_queue()
    queue = [item for item in queue if item.get('id') != queue_id]
    with open(SYNC_QUEUE_FILE, 'w') as f:
        json.dump(queue, f, ensure_ascii=False)

def process_sync_queue(queue):
    """执行前端提交的待同步操作，直接修改 md 文件"""
    for item in queue:
        try:
            op_type = item.get('type', '')
            if op_type == 'toggle-task':
                idx = item.get('index', -1)
                target_done = item.get('done')
                # 直接修改 md 文件中的任务状态
                result = toggle_today_task(idx)
                # 如果目标状态和当前状态不一致，再 toggle 一次
                if result.get('ok') and result.get('done') != target_done:
                    toggle_today_task(idx)
            elif op_type == 'toggle-milestone':
                goal_file = item.get('goal_file', '')
                idx = item.get('index', -1)
                target_done = item.get('done')
                result = toggle_goal_milestone(goal_file, idx)
                if result.get('ok') and result.get('done') != target_done:
                    toggle_goal_milestone(goal_file, idx)
            elif op_type == 'failure-review':
                write_failure_review({
                    'task_text': item.get('task_text', ''),
                    'reason': item.get('reason', ''),
                    'learnings': item.get('learnings', ''),
                })
        except Exception as e:
            print(f'[sync] Error processing {item.get("type")}: {e}')

# ─── HTTP 服务器（提供 JSON API + 静态文件） ───────────────────

class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, *')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _send_file(self, filepath, content_type='text/html; charset=utf-8'):
        if not os.path.isfile(filepath):
            self.send_error(404, 'File not found')
            return
        with open(filepath, 'rb') as f:
            content = f.read()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        self._send_json({'ok': True})

    def do_GET(self):
        if self.path.startswith('/api/data'):
            # 支持前端通过 ?sync= 参数提交待同步操作
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            sync_param = params.get('sync', [''])[0]
            if sync_param:
                try:
                    queue = json.loads(sync_param)
                    if isinstance(queue, list):
                        process_sync_queue(queue)
                except (json.JSONDecodeError, Exception):
                    pass
            data = collect_data()
            self._send_json(data)
        elif self.path == '/api/files':
            self._send_json({'files': list_uploaded_files()})
        elif self.path == '/api/sync-queue':
            self._send_json(get_sync_queue())
        else:
            # Map URL path to local file
            url_path = self.path.split('?')[0]  # strip query string
            if url_path == '/':
                url_path = '/dashboard.html'
            # Security: prevent directory traversal
            filepath = os.path.normpath(os.path.join(BASE_DIR, url_path.lstrip('/')))
            if not filepath.startswith(BASE_DIR):
                self.send_error(403, 'Forbidden')
                return
            # Determine content type
            ext = os.path.splitext(filepath)[1].lower()
            content_types = {
                '.html': 'text/html; charset=utf-8',
                '.css': 'text/css; charset=utf-8',
                '.js': 'application/javascript; charset=utf-8',
                '.json': 'application/json; charset=utf-8',
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.svg': 'image/svg+xml',
            }
            ct = content_types.get(ext, 'application/octet-stream')
            self._send_file(filepath, ct)

    def do_POST(self):
        try:
            content_type = self.headers.get('Content-Type', '')

            # ── 文件上传（multipart/form-data）──
            if 'multipart/form-data' in content_type:
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        'REQUEST_METHOD': 'POST',
                        'CONTENT_TYPE': content_type,
                    }
                )
                file_item = form['file']
                if not file_item.filename:
                    self._send_json({'ok': False, 'error': '未选择文件'}, 400)
                    return
                file_data = file_item.file.read()
                result = upload_file(file_item.filename, file_data)
                self._send_json(result)
                return

            # ── JSON API ──
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length).decode('utf-8')) if length > 0 else {}

            if self.path == '/api/toggle-task':
                idx = body.get('index', -1)
                if idx < 0:
                    self._send_json({'ok': False, 'error': '缺少 index 参数'}, 400)
                    return
                result = toggle_today_task(idx)
                self._send_json(result)

            elif self.path == '/api/toggle-milestone':
                goal_file = body.get('goal_file', '')
                idx = body.get('index', -1)
                if not goal_file or idx < 0:
                    self._send_json({'ok': False, 'error': '缺少 goal_file 或 index 参数'}, 400)
                    return
                result = toggle_goal_milestone(goal_file, idx)
                self._send_json(result)

            elif self.path == '/api/delete-file':
                filename = body.get('filename', '')
                if not filename:
                    self._send_json({'ok': False, 'error': '缺少 filename 参数'}, 400)
                    return
                result = delete_file(filename)
                self._send_json(result)

            elif self.path == '/api/save-daily-summary':
                review_path = write_daily_review(body if isinstance(body, dict) else {})
                self._send_json({'ok': True, 'review_path': review_path})

            elif self.path == '/api/save-failure-review':
                result = write_failure_review(body if isinstance(body, dict) else {})
                self._send_json(result)

            elif self.path == '/api/ack-sync':
                queue_id = body.get('id', '')
                if queue_id:
                    ack_sync_item(queue_id)
                self._send_json({'ok': True})

            else:
                self._send_json({'ok': False, 'error': '未知 API'}, 404)

        except json.JSONDecodeError:
            self._send_json({'ok': False, 'error': '无效的 JSON'}, 400)
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)}, 500)

    def log_message(self, format, *args):
        msg = str(args[0]) if args else ''
        if '/api/data' not in msg:
            super().log_message(format, *args)

def serve():
    """启动 HTTP 服务器"""
    generate_html()
    server = HTTPServer(('0.0.0.0', PORT), DashboardHandler)
    console_print(f"\n🚀 Dashboard server running at:")
    console_print(f"   http://localhost:{PORT}")
    console_print(f"   Press Ctrl+C to stop\n")

    def shutdown(sig, frame):
        console_print("\n🛑 Server stopped.")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    server.serve_forever()

# ─── 入口 ──────────────────────────────────────────────────────

if __name__ == '__main__':
    if '--serve' in sys.argv or '-s' in sys.argv:
        serve()
    else:
        generate_html()
        console_print(f"\n💡 提示: 运行 python3 {os.path.basename(__file__)} --serve 可启动实时服务器")
