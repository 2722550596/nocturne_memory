import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/**
 * 支持的思维链标签列表。
 * 如果未来需要支持更多标签（如 "reasoning"），只需在此数组中添加即可。
 */
const SUPPORTED_TAGS = ["think", "thinking"];

export default function (pi: ExtensionAPI) {
  pi.on("message_end", (event) => {
    const msg = event.message;
    if (msg.role !== "assistant") return;
    if (!msg.content || msg.content.length === 0) return;

    let changed = false;
    const newContent: any[] = [];

    for (const block of msg.content) {
      if (block.type !== "text") {
        newContent.push(block);
        continue;
      }

      const text = block.text;

      // 1. 寻找最靠前的闭合标签 (处理一条消息中包含多种闭合标签的极端情况)
      let matchedTag: string | null = null;
      let closeIdx = -1;

      for (const tag of SUPPORTED_TAGS) {
        const closeTag = `</${tag}>`;
        const idx = text.indexOf(closeTag);
        if (idx !== -1 && (closeIdx === -1 || idx < closeIdx)) {
          closeIdx = idx;
          matchedTag = tag;
        }
      }

      // 2. 如果没有找到任何闭合标签
      if (closeIdx === -1) {
        let isDoubleOpenBug = false;

        // 检查双开头标签异常 (如 <think>\n <think> 或 <thinking>\n <think> 且无闭合)
        for (const tag1 of SUPPORTED_TAGS) {
          const open1 = `<${tag1}>`;
          if (text.startsWith(open1)) {
            const restText = text.slice(open1.length).trimStart();
            for (const tag2 of SUPPORTED_TAGS) {
              const open2 = `<${tag2}>`;
              if (restText.startsWith(open2)) {
                isDoubleOpenBug = true;
                break;
              }
            }
          }
          if (isDoubleOpenBug) break;
        }

        if (isDoubleOpenBug) {
          // 渠道漏了闭合标签，只剩双开头标签，清空 content
          return { message: { ...msg, content: [] } };
        }

        // 正常内容，原样保留
        newContent.push(block);
        continue;
      }

      // 3. 找到了闭合标签，进行切分
      const closeTagStr = `</${matchedTag}>`;
      
      // 闭合标签之前的内容（含可能的开头标签）
      let thinking = text.slice(0, closeIdx).trim();
      
      // 洗掉可能存在的开头标签 (遍历所有支持的标签以兼容模型前后标签不一致的情况)
      for (const tag of SUPPORTED_TAGS) {
        const openTag = `<${tag}>`;
        if (thinking.startsWith(openTag)) {
          thinking = thinking.slice(openTag.length).trimStart();
          break; 
        }
      }

      // 闭合标签之后的内容（即正文）
      const rest = text.slice(closeIdx + closeTagStr.length).trimStart();

      // 只有当 thinking 部分非空时才创建 thinking block
      if (thinking.length > 0) {
        newContent.push({
          type: "thinking",
          thinking,
          thinkingSignature: "reasoning",
        });
      }
      if (rest.length > 0) {
        newContent.push({ type: "text", text: rest });
      }
      changed = true;
    }

    if (!changed) return;

    return { message: { ...msg, content: newContent } };
  });
}