# nocturne autorecall 设计定稿（2026-09-02 与明月敲定）

## 模型
角色在回复尾部自发输出 `<recall>…</recall>`（反思/内心浮现，疑问句形式佳，如
`<recall>xxx在那个教室做什么了？</recall>`）。这是**复盘式涌现**（说完才想起），
不是预判式检索。时序：assistant 回复落定 → 扩展 parse → 独立检索 → 独立注入。

## 三路架构（每路独立完整，不做跨路分数合并）
- prompt 路：user 原话，带 BGE instruction
- context 路：Prior context 块（已实现），不带 instruction
- recall 路（未来）：`<recall>` 内容作为 query，走完整 recall()
  （MIN_SCORE 0.35 / TOP_K / anchor 全套），产出**独立的 rp-memories 消息**

## 实现链路（API 已在 pi-coding-agent dist 源码验证）
- 挂 `agent_settled`（优于 agent_end：避开 queued/compaction/retry 竞态），
  从 `event.messages` 最后 assistant 文本 parse `<recall>`
- 注入用扩展 api 顶层 `sendMessage({customType:"rp-memories",...}, {triggerTurn:false, deliverAs:"followUp"})`
- 极端竞态（settled 未返回用户新消息已到）：接受迟到一轮，不做等待逻辑

## 去重（天然覆盖，无需新代码）
recall 路注入的消息带 details.ids/hashes → rebuildInjectedFromSession 扫全部
历史 rp-memories → injected map 自动收录 → 后续轮次三路都不再重复注入。
context 路跳过含 `<memories>` 的条目 → 记忆链不自激。

## `<recall>` 标签处理
- 留在历史与 LLM payload（LLM 看到自己上轮反思，连贯性）
- 角色卡解释为"内心浮现/反思"动作
- 用户不可见：pi-rp preset regex rule（stage: compiled, effect: display,
  roles: ["assistant"], pattern: `\s*<recall>[\s\S]*?</recall>`, replace: ""）

## 相关长期方向（另已 retain）
- 数据飞轮：记录 query→召回→是否被采用，为微调攒对比对
- per-character embedding 微调（LoRA on BGE，本地 ONNX 推理为前提），
  与 autorecall 殊途同归：让检索路径被角色经历塑造
