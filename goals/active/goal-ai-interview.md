---
id: goal-ai-interview
title: AI应用开发面试准备
status: active
created_at: 2026-04-25
updated_at: 2026-04-25
parent: null
children: []
time:
  estimated_duration: "约9周（4月25日 ~ 6月30日）"
  deadline: "2026-06-30"
  started_at: 2026-04-25
  total_days: 66
  remaining_days: 66
progress_method: milestone
progress: "未开始"
related_tasks: []
tags:
  - AI
  - 面试
  - 开发
  - 学习

## 目标描述
在6月30日前完成AI应用开发面试准备，具备通过面试的能力。

## 为什么重要
AI是当前最热门的技术方向，掌握AI应用开发能力可以打开更多职业机会。

## 成功标准（量化）
- [ ] 掌握 ≥ 15个核心知识点（LLM、Prompt、RAG、Agent、向量数据库等）
- [ ] 完成 ≥ 3个实战项目
- [ ] 算法刷题 ≥ 50道
- [ ] 完成 ≥ 5次模拟面试
- [ ] 能流畅回答常见面试问题（自我评估通过率 ≥ 80%）

## 倒推计划
| 周次 | 日期范围 | 重点 | 每日任务量 | 检查标准 |
|------|---------|------|-----------|---------|
| W1 | 4/25~5/1 | 梳理知识点 | 梳理完整清单+学习2个知识点 | 知识点清单完成 |
| W2 | 5/2~5/8 | 核心知识（上） | 学习3个知识点+1个小练习 | 掌握5个知识点 |
| W3 | 5/9~5/15 | 核心知识（下） | 学习3个知识点+1个小练习 | 掌握8个知识点 |
| W4 | 5/16~5/22 | 项目实战1 | 完成第1个实战项目 | 项目1完成可演示 |
| W5 | 5/23~5/29 | 项目实战2 | 完成第2个实战项目 | 项目2完成可演示 |
| W6 | 5/30~6/5 | 项目实战3+算法 | 完成第3个项目+刷15题 | 项目3完成+15题 |
| W7 | 6/6~6/12 | 算法+模拟面试 | 刷20题+2次模拟面试 | 模拟面试通过 |
| W8 | 6/13~6/19 | 查漏补缺 | 补弱项+刷15题+2次模拟 | 弱项补齐 |
| W9 | 6/20~6/30 | 冲刺 | 复习+最终模拟面试 | 准备就绪 |

## 🎯 学习路线图（基于面试高频考点）

### 四阶段学习路线
| 阶段 | 时间 | 重点 | 产出 |
|------|------|------|------|
| 阶段0 | W1 | Python基础 + API调用 + LLM概念 | 能调OpenAI API做问答 |
| 阶段1 | W2-W4 | Prompt + RAG + Agent | 完成项目1：RAG知识库问答 |
| 阶段2 | W5-W7 | 工程化 + 部署 + 评估 | 完成项目2：多工具Agent系统 |
| 阶段3 | W8-W9 | 微调 + 系统设计 + 面试 | 模拟面试通过 |

### P0 必须精通（面试必问）
1. LLM基础：Transformer自注意力、预训练vs微调、Token、位置编码
2. Prompt Engineering：角色设定、Few-shot、CoT、temperature/top-p
3. RAG全链路：文档切割、Embedding、向量数据库、检索+重排序、引用溯源
4. Agent核心：ReAct、Function Calling、工具定义、Memory
5. LangChain/LlamaIndex：核心组件、LCEL、LangGraph
6. 工程开发：Python异步、FastAPI、RESTful API、SSE流式、Docker

### 推荐实战项目
1. **企业文档智能问答（RAG）**：PDF解析+FAISS+混合检索+引用溯源+FastAPI
2. **多工具智能体（Agent）**：ReAct+Function Calling+多工具集成+记忆管理
3. **模型服务部署（工程）**：vLLM/Ollama+量化+监控+压测报告

### 面试高频考点（按出现率排序）
- RAG原理与难点（文档切割、幻觉、微调vs RAG）
- Agent设计（ReAct、Function Calling、多Agent）
- LLM基础（Transformer、微调方案、Embedding）
- Prompt工程（CoT、参数调优）
- 框架对比（LangChain vs 手搓、MCP vs Function Calling）

### 关键资源
- GitHub: datawhalechina/hello-agents（面试真题+答案）
- LangChain/LlamaIndex 官方文档
- 3Blue1Brown 神经网络系列（B站）
- 吴恩达 AI For Everyone（Coursera）

## 每周检查点
- 每周回顾知识点掌握情况
- 项目进度是否达标
- 如果某周进度落后50%以上，触发重规划

## 子目标
1. 梳理面试知识点清单（W1）
2. 系统学习核心知识（W1-W3，15个知识点）
3. 项目实战（W4-W6，3个项目）
4. 算法刷题（W6-W8，50道）
5. 模拟面试（W7-W9，5次）

## 里程碑
- [ ] 知识点清单完成（W1）
- [ ] 掌握8个核心知识点（W3结束）
- [ ] 完成3个实战项目（W6结束）
- [ ] 算法刷题50道（W8结束）
- [ ] 完成5次模拟面试（W9结束）
- [ ] 面试准备就绪（6月30日）

## 备注
- 可引用 my-knowledge-system/ 中的AI知识库
- 知识点清单需要根据目标岗位JD调整
- 项目选题需兼顾展示性和实用性
- 与四级考试并行（6月13日四级考完后可全力投入）
