# 目标拆解提示词

## 使用说明
创建新目标或拆解现有目标时使用。

---

## 提示词

```
你正在执行目标拆解。

将用户的目标拆解为可执行的子目标和每日任务。

拆解原则：
1. 语义理解优先：理解目标的真正含义，不是机械按时间切分
2. 最小可感知：每个任务完成后能明确说"我做了"
3. 有明确边界：知道什么时候算完成
4. 时间可控：单个任务不超过30分钟
5. 可量化：有数字可以追踪

拆解流程：
1. 理解目标含义和约束
2. 确定时间规划（estimated_duration + deadline）
3. 拆解为 2-4 个子目标（如有必要）
4. 将最细粒度的子目标拆解为每日任务
5. 为每个任务分配 XP 值
6. 确定任务之间的依赖关系
7. 检查失败记忆，规避已知风险

输出：
1. 目标文件 → goals/active/goal-{{slug}}.md
2. 更新 goals/_index.md
3. 生成首批每日任务 → tasks/daily/YYYY/MM/YYYY-MM-DD.md
4. 更新 tasks/_index.md

目标文件格式：
---
id: goal-{{slug}}
title: {{目标名称}}
status: active | completed | paused
created_at: YYYY-MM-DD
updated_at: YYYY-MM-DD
parent: {{父目标ID（如有）}}
children: [{{子目标ID列表}}]
time:
  estimated_duration: "{{估算时长}}"
  deadline: "{{硬性截止（如有）}}"
  started_at: YYYY-MM-DD
progress_method: percentage | milestone
progress: "{{当前进度描述}}"
milestones:
  - name: "{{里程碑1}}"
    done: false
  - name: "{{里程碑2}}"
    done: false
related_tasks: [{{关联任务ID列表}}]
tags: [{{标签}}]

## 目标描述
{{详细描述这个目标}}

## 为什么重要
{{这个目标对用户的意义}}

## 成功标准
{{怎么算达成}}

## 子目标
1. {{子目标1}} → goal-{{slug1}}
2. {{子目标2}} → goal-{{slug2}}

## 里程碑
- [ ] {{里程碑1}}
- [ ] {{里程碑2}}
- [ ] {{里程碑3}}

## 备注
{{其他补充}}
```
