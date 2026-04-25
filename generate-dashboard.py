#!/usr/bin/env python3
"""Stone Plan Dashboard Generator
扫描 stone-plan 目录下所有 md 文件，生成一个自包含的可视化仪表盘 HTML。
支持动态刷新：内置 HTTP 服务器，HTML 页面每 10 秒自动拉取最新数据。
"""

import os
import re
import json
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
PORT = 8765

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
    return {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'goals': goals,
        'today': today,
        'quicklog': quicklog,
        'health': health,
        'finance': finance,
        'weekly_plan': weekly_plan,
        'memory': memory,
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
    color: #cbd5e1;
    border-radius: 6px;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    transition: background 0.2s;
    user-select: none;
  }}
  .task-item:hover {{ background: #334155; }}
  .task-item:active {{ transform: scale(0.98); }}
  .task-item.done {{ color: #475569; text-decoration: line-through; }}
  .task-item.syncing {{ opacity: 0.5; pointer-events: none; }}
  .task-item .check-icon {{ transition: transform 0.2s; }}
  .task-item:hover .check-icon {{ transform: scale(1.2); }}
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
    border-radius: 20px;
    font-size: 13px;
    font-weight: 500;
    color: #94a3b8;
    text-decoration: none;
    background: rgba(255,255,255,0.05);
    border: 1px solid transparent;
    transition: all 0.2s;
    scroll-margin-top: 60px;
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

  <!-- Timeline -->
  <div class="card full-width" id="section-timeline">
    <div class="card-title"><span class="icon">📅</span> 时间线</div>
    <div id="timeline"></div>
  </div>

  <div class="grid">
    <!-- Today -->
    <div class="card" id="section-today">
      <div class="card-title"><span class="icon">📋</span> 今日任务 <span id="todayDate" style="font-size:12px;color:#94a3b8;font-weight:400;margin-left:8px;"></span></div>
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

// ─── 渲染函数 ──────────────────────────────────────
function render(data) {{
  document.getElementById('updateTime').textContent = '最后更新: ' + data.generated_at;
  renderStats(data);
  renderTimeline(data);
  renderGoals(data.goals);
  renderToday(data.today);
  renderQuicklog(data.quicklog);
  renderWeekly(data.weekly_plan);
  renderHealth(data.health);
  renderFinance(data.finance);
}}

function renderStats(data) {{
  const goals = data.goals || [];
  const today = data.today;
  const activeGoals = goals.filter(g => g.status === '进行中').length;
  const totalTasks = today ? today.total : 0;
  const doneTasks = today ? today.done : 0;
  const logs = (data.quicklog || []).length;
  const pct = totalTasks > 0 ? Math.round(doneTasks / totalTasks * 100) : 0;

  document.getElementById('statsBar').innerHTML = `
    <div class="stat-item">
      <div class="stat-value">${{activeGoals}}</div>
      <div class="stat-label">🎯 进行中目标</div>
    </div>
    <div class="stat-item">
      <div class="stat-value">${{doneTasks}}/${{totalTasks}}</div>
      <div class="stat-label">📋 今日任务</div>
    </div>
    <div class="stat-item">
      <div class="stat-value">${{pct}}%</div>
      <div class="stat-label">📊 今日完成率</div>
    </div>
    <div class="stat-item">
      <div class="stat-value">${{logs}}</div>
      <div class="stat-label">📝 QuickLog记录</div>
    </div>
  `;
}}

function renderTimeline(data) {{
  const goals = (data.goals || []).filter(g => g.deadline && g.deadline !== '未设定');
  // Sort by remaining days (nearest deadline first)
  goals.sort((a, b) => (a.remaining || 999) - (b.remaining || 999));
  let items = '<div class="tl-item"><div class="tl-dot now"></div><div class="tl-label">今天<br><small>' +
    new Date().toLocaleDateString('zh-CN') + '</small></div></div>';
  for (const g of goals) {{
    items += `<div class="tl-item"><div class="tl-dot deadline"></div><div class="tl-label">${{g.emoji}} ${{g.title}}<br><small>${{g.deadline}} (${{g.remaining}}天)</small></div></div>`;
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

    html += `
    <div class="goal-card fade-in" style="border-left-color:${{g.status_color}}">
      <div class="goal-header">
        <span class="goal-emoji">${{g.emoji}}</span>
        <span class="goal-title">${{g.title}}</span>
        <span class="goal-status" style="background:${{g.status_color}}">${{g.status}}</span>
      </div>
      <div class="goal-meta">
        <span>📅 ${{g.deadline}}</span>
        <span>⏰ 剩余: ${{g.remaining}}天</span>
        <span>📊 ${{g.progress}}</span>
      </div>
      <div class="progress-bar">
        <div class="progress-fill ${{fillClass}}" style="width:${{pct}}%"></div>
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
    <div class="progress-label">今日进度: ${{today.done}}/${{today.total}} (${{today.pct}}%)</div>
    <div class="progress-bar large">
      <div class="progress-fill ${{today.pct >= 80 ? 'green' : today.pct >= 40 ? '' : 'red'}}" style="width:${{today.pct}}%"></div>
    </div>
  </div>`;

  // Task list
  html += '<div class="task-list">';
  for (let ti = 0; ti < today.tasks.length; ti++) {{
    const t = today.tasks[ti];
    const icon = t.done ? '✅' : '⬜';
    html += `<div class="task-item ${{t.done ? 'done' : ''}}" onclick="toggleTask(${{ti}}, this)" title="点击切换完成状态"><span class="check-icon">${{icon}}</span> ${{t.text}}</div>`;
  }}
  html += '</div>';

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
function toggleTask(index, el) {{
  if (el.classList.contains('syncing')) return;
  el.classList.add('syncing');
  // Optimistic UI update
  const wasDone = el.classList.contains('done');
  el.querySelector('.check-icon').textContent = wasDone ? '⬜' : '✅';
  el.classList.toggle('done', !wasDone);

  fetch('/api/toggle-task', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ index: index }})
  }})
  .then(r => r.json())
  .then(result => {{
    el.classList.remove('syncing');
    if (result.ok) {{
      setTimeout(fetchAndUpdate, 500);
    }} else {{
      // Revert on error
      el.querySelector('.check-icon').textContent = wasDone ? '✅' : '⬜';
      el.classList.toggle('done', wasDone);
    }}
  }})
  .catch(err => {{
    el.classList.remove('syncing');
    // Revert on error
    el.querySelector('.check-icon').textContent = wasDone ? '✅' : '⬜';
    el.classList.toggle('done', wasDone);
  }});
}}

function toggleMilestone(goalFile, index, el) {{
  el.classList.add('syncing');
  fetch('/api/toggle-milestone', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ goal_file: goalFile, index: index }})
  }})
  .then(r => r.json())
  .then(result => {{
    if (result.ok) {{
      const icon = result.done ? '✅' : '⬜';
      el.firstChild.textContent = icon + ' ';
      el.classList.toggle('done', result.done);
      setTimeout(fetchAndUpdate, 300);
    }} else {{
      el.classList.remove('syncing');
    }}
  }})
  .catch(err => {{
    el.classList.remove('syncing');
  }});
}}

// ─── 动态刷新 ──────────────────────────────────────
function fetchAndUpdate() {{
  fetch('/api/data')
    .then(r => r.json())
    .then(data => {{
      dashboardData = data;
      render(data);
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
    const target = document.querySelector(pill.getAttribute('href'));
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
    print(f"✅ Dashboard generated: {OUTPUT_HTML}")
    print(f"   Goals: {len(data['goals'])}")
    if data['today']:
        print(f"   Today tasks: {data['today']['done']}/{data['today']['total']}")
    else:
        print("   Today tasks: N/A")
    print(f"   QuickLog entries: {len(data['quicklog'])}")

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
        if self.path == '/api/data':
            data = collect_data()
            self._send_json(data)
        elif self.path == '/api/files':
            self._send_json({'files': list_uploaded_files()})
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
    print(f"\n🚀 Dashboard server running at:")
    print(f"   http://localhost:{PORT}")
    print(f"   Press Ctrl+C to stop\n")

    def shutdown(sig, frame):
        print("\n🛑 Server stopped.")
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
        print(f"\n💡 提示: 运行 python3 {os.path.basename(__file__)} --serve 可启动实时服务器")
