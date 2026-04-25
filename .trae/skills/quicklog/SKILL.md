---
name: QuickLog
description: 一句话快速记录。当用户说"我做了XX"、"记录"、"花了XX"、"跑了XX"等描述已完成事项的语句时自动触发。
---

# QuickLog

## 描述
Stone Plan 的快速记录技能。用户说一句话，解析并记录到系统，同时触发即时反馈。

## 使用场景
- 用户说"我跑了30分钟"、"花了45块吃饭"、"背了50个单词"
- 用户说"记录一下"、"记一下"
- 用户描述任何已完成的动作或事件

## 指令

### 启动时必读
1. AGENTS.md
2. goals/_index.md
3. tasks/daily/ 今天的任务文件
4. memory/patterns.md

### 解析规则
1. 判断类型：exercise | expense | mood | task | note | weight | sleep | study | diet
2. 提取关键数据（时长、金额、数值）
3. 判断关联的目标或任务
4. 如果是任务完成，触发即时反馈

### diet 类型规则
- 饮食记录：食物 + 热量 + 蛋白质 + 碳水估算
- 用户发图片时，识别食物内容并估算热量/蛋白质/碳水
- 图片识别结果需标注"估算"字样，数值仅供参考
- 写入 health/diet/ 目录

### 输出步骤
1. 写入 quicklog/YYYY/MM/ql-YYYY-MM-DD-{{序号}}.md（序号自动递增）
2. 如果是任务完成，更新 tasks/daily/ 今日文件中的任务状态
3. 更新相关目标进度（goals/active/ 和 goals/_index.md）
4. 更新健康/财务数据文件（如 health/exercise.md、finance/expense.md）
5. 触发即时反馈

### QuickLog 文件格式
```markdown
---
id: ql-YYYY-MM-DD-{{序号}}
date: YYYY-MM-DD
time: HH:MM
type: {{类型}}
related_goal: {{目标ID}}
related_task: {{任务ID}}

## 记录
{{用户说的话}}

## 数据
{{结构化数据}}

## 标签
{{自动标签}}
```

## 示例

**用户**：我跑了30分钟
**AI**：
✅ 跑步30分钟
+25 XP（中型任务）
🔥 跑步连击：6天（+10% XP加成）

📊 今日进度：█████████████░░░░ 75%（6/8）

🎯 减脂目标：158→预估157.8斤 ████████████░░░░░░░░ 40%

🎮 下一步：
1. 🥗 记录午餐饮食 [10XP]
2. 💧 下午喝水提醒
3. 📖 晚上：背单词（🔥 连击12天，别断！）
