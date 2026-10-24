"use strict";

// The copy/export source is committed only after every page of one snapshot
// has arrived. Polls never turn a partial response into a complete document.
((root) => {
  async function readSnapshot(fetchPage, source, previousRevision = null) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const first = await fetchPage({source, cursor: null});
      if (previousRevision && first.revision === previousRevision) return null;
      let page = first;
      const items = [], ids = new Set(), cursors = new Set();
      let changed = false;
      while (true) {
        if (!Array.isArray(page.items) || !Number.isInteger(page.total) || page.total < 0
            || typeof page.revision !== "string" || !Number.isFinite(page.snapshot)) {
          throw new Error("完整转写返回格式异常，请刷新重试。");
        }
        if (page.snapshot !== first.snapshot || page.revision !== first.revision || page.total !== first.total) {
          changed = true; break;
        }
        for (const item of page.items) {
          if (!item.id || typeof item.text !== "string" || ids.has(item.id)) {
            throw new Error("转写分页不完整，请刷新后再复制全文。");
          }
          ids.add(item.id); items.push(item);
        }
        if (!page.next_cursor) break;
        if (!page.items.length || cursors.has(page.next_cursor) || items.length >= first.total) {
          throw new Error("转写分页不完整，请刷新后再复制全文。");
        }
        cursors.add(page.next_cursor);
        page = await fetchPage({source, cursor: page.next_cursor});
      }
      if (!changed && items.length === first.total) return {...first, items};
    }
    throw new Error("转写记录正在更新，本次尚未读取完整。请稍后刷新再复制全文。");
  }

  function paragraphs(item) {
    const text = item.text || "";
    const segments = Array.isArray(item.segments) ? item.segments.filter(s => typeof s.text === "string" && s.text.trim()) : [];
    // An analyzed file can have missing or imperfect ASR timestamps. Preserve
    // its complete text and report that distinction without guessing a speaker.
    const analyzed = Number.isInteger(item.speaker_count) && item.speaker_count >= 0;
    const fallbackLabel = !analyzed ? "" : item.speaker_count === 0
      ? "未检测到说话人" : `已区分 ${item.speaker_count} 位说话人 · 逐句归属未对齐`;
    const fallback = [{text, label: fallbackLabel}];
    if (!segments.some(s => Array.isArray(s.speaker_labels) && s.speaker_labels.length)) return fallback;
    const starts = [];
    let offset = 0;
    for (const segment of segments) {
      const part = segment.text.trim(), start = text.indexOf(part, offset);
      // Any mismatch uses the authoritative full text, never a lossy segment sum.
      if (start < 0 || text.slice(offset, start).trim()) return fallback;
      starts.push(start); offset = start + part.length;
    }
    if (text.slice(offset).trim()) return fallback;
    const result = [];
    let previousSpeakers = null;
    segments.forEach((segment, index) => {
      const labels = Array.isArray(segment.speaker_labels) ? [...new Set(segment.speaker_labels.filter(label => typeof label === "string"))] : [];
      const ids = Array.isArray(segment.speaker_ids) ? [...new Set(segment.speaker_ids.filter(id => typeof id === "string"))].sort() : [];
      const label = ids.length > 1 || labels.length > 1 ? `混合发言（${labels.join("、") || "未命名"}）` : labels[0] || "未区分";
      // Two different people may share a name and note. Display labels are not
      // identities, so retain their turn boundary and overlap indication.
      const speakers = ids.length ? `ids:${JSON.stringify(ids)}` : `labels:${JSON.stringify(labels)}`;
      const part = text.slice(index === 0 ? 0 : starts[index], index + 1 < starts.length ? starts[index + 1] : text.length);
      const last = result[result.length - 1];
      if (last?.label === label && previousSpeakers === speakers) last.text += part;
      else result.push({text: part, label});
      previousSpeakers = speakers;
    });
    return result.length ? result : fallback;
  }

  function stamp(value, date = false) {
    const valueDate = new Date(value * 1000);
    if (!Number.isFinite(valueDate.getTime())) return "时间未记录";
    return new Intl.DateTimeFormat("zh-CN", date
      ? {year:"numeric", month:"2-digit", day:"2-digit"}
      : {hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false}).format(valueDate);
  }

  const timeRange = item => `${stamp(item.start_time)}–${stamp(item.end_time)}`;
  const plainText = items => items.map(item => `${stamp(item.start_time, true)} ${timeRange(item)}\n${item.text || "（未识别到文字）"}`).join("\n\n");
  const exported = {readSnapshot, paragraphs, stamp, timeRange, plainText};
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else root.AudioDocument = exported;
})(typeof globalThis !== "undefined" ? globalThis : this);
