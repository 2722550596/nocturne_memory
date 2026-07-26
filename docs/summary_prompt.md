<skill name="Memory Summary Prompt">

## 记忆摘要协议

当需要总结当前会话中的关键记忆时，使用以下格式：

### 核心事件
- [时间] [事件摘要] [关联 URI]

### 角色状态变化
- [变化前] → [变化后] [关联 URI]

### 重要关系
- [关系描述] [关联 URI]

### 待办/待处理
- [事项] [优先级] [关联 URI]

总结后写入到 `core://agent/session_logs/{date}` 路径下。

</skill>