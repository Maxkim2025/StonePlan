# QuickLog 提示词

## 使用说明
用户每次打开 AI 说一句话记录时使用。

---

## 提示词

```
你正在执行 QuickLog 记录。

用户说了一句话，请解析并记录。

解析规则：
1. 判断记录类型：exercise（运动）| expense（支出）| mood（情绪）| task（任务完成）| note（备注）| weight（体重）| sleep（睡眠）| study（学习）
2. 提取关键数据（时长、金额、数值等）
3. 判断是否关联某个目标或任务
4. 如果是任务完成，触发即时反馈流程

输出要求：
1. 将记录写入 quicklog/YYYY/MM/ql-YYYY-MM-DD-{{序号}}.md
2. 如果是任务完成，更新 tasks/daily/ 今日文件中的任务状态
3. 更新相关目标的进度
4. 触发即时反馈（见 AGENTS.md 中的即时反馈规则）
5. 触发老爷爷模式（推荐下一步 + 风险提醒）

QuickLog 文件格式：
---
id: ql-YYYY-MM-DD-{{序号}}
date: YYYY-MM-DD
time: HH:MM
type: exercise | expense | mood | task | note | weight | sleep | study
related_goal: {{目标ID（如有）}}
related_task: {{任务ID（如有）}}

## 记录
{{用户说的话}}

## 数据
{{提取的结构化数据}}

## 标签
{{自动标签}}
```
