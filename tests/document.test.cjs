"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {readSnapshot, paragraphs, plainText} = require("../audio_agent/web_assets/document.js");

const SOURCE = "live:测试会话";
const SNAPSHOT = 1_750_000_000;

test("same display name does not merge turns belonging to different speaker IDs", () => {
  const item = {text:"  第一人。\n第二人。  ", segments:[
    {text:"第一人。", speaker_labels:["小王 · 同事"], speaker_ids:["speaker_1"]},
    {text:"第二人。", speaker_labels:["小王 · 同事"], speaker_ids:["speaker_2"]},
  ]};
  const result = paragraphs(item);
  assert.equal(result.length, 2);
  assert.deepEqual(result.map(part => part.label), ["小王 · 同事", "小王 · 同事"]);
  assert.equal(result.map(part => part.text).join(""), item.text);
});

test("overlapping speaker IDs remain mixed even when their names are identical", () => {
  const item = {text:"一起说话。", segments:[
    {text:"一起说话。", speaker_labels:["小王", "小王"], speaker_ids:["speaker_1", "speaker_2"]},
  ]};
  assert.deepEqual(paragraphs(item), [{text:item.text, label:"混合发言（小王）"}]);
});

test("same speaker set merges regardless of ID ordering without altering whitespace", () => {
  const item = {text:"一段\n\n下一段 ", segments:[
    {text:"一段", speaker_labels:["小王"], speaker_ids:["speaker_2", "speaker_1"]},
    {text:"下一段", speaker_labels:["小王"], speaker_ids:["speaker_1", "speaker_2", "speaker_1"]},
  ]};
  assert.deepEqual(paragraphs(item), [{text:item.text, label:"混合发言（小王）"}]);
});

function record(index, text = `唯一段落 ${index}：完整内容。`) {
  return {id: `transcript-${index}`, text, start_time: SNAPSHOT + index,
    end_time: SNAPSHOT + index + 1, segments: []};
}

function page(items, total, next_cursor = null, revision = "revision-1", snapshot = SNAPSHOT) {
  return {items, total, next_cursor, revision, snapshot};
}

function sequence(entries) {
  const calls = [];
  const fetchPage = async query => {
    const index = calls.length;
    calls.push(query);
    assert.ok(index < entries.length, "unexpected extra page request");
    const entry = entries[index];
    if (entry instanceof Error) throw entry;
    return entry;
  };
  return {fetchPage, calls};
}

test("reads over 200 records across three pages before exporting all text", async () => {
  const items = Array.from({length: 451}, (_, index) => record(index));
  items[200].text = `长段开头\n${"保留全部文字和空格。 ".repeat(1800)}\n长段结尾`;
  const {fetchPage, calls} = sequence([
    page(items.slice(0, 200), items.length, "page-2"),
    page(items.slice(200, 400), items.length, "page-3"),
    page(items.slice(400), items.length),
  ]);
  const result = await readSnapshot(fetchPage, SOURCE);
  assert.deepEqual(result.items, items);
  assert.equal(result.total, 451);
  assert.deepEqual(calls, [
    {source: SOURCE, cursor: null}, {source: SOURCE, cursor: "page-2"},
    {source: SOURCE, cursor: "page-3"},
  ]);
  const copied = plainText(result.items);
  let previousEnd = 0;
  for (const item of items) {
    const start = copied.indexOf(item.text, previousEnd);
    assert.ok(start >= previousEnd, `missing or reordered transcript ${item.id}`);
    previousEnd = start + item.text.length;
  }
  assert.ok(copied.endsWith(items.at(-1).text));
});

test("second-page failure rejects instead of returning partial copyable text", async () => {
  const failure = new Error("page two is unavailable");
  const {fetchPage, calls} = sequence([page([record(0)], 2, "page-2"), failure]);
  let returned;
  await assert.rejects(async () => { returned = await readSnapshot(fetchPage, SOURCE); },
    error => error === failure);
  assert.equal(returned, undefined);
  assert.equal(calls.length, 2);
});

test("repeated cursor rejects before entering a pagination loop", async () => {
  const {fetchPage, calls} = sequence([
    page([record(0)], 3, "same-cursor"),
    page([record(1)], 3, "same-cursor"),
  ]);
  await assert.rejects(readSnapshot(fetchPage, SOURCE), /分页不完整/);
  assert.equal(calls.length, 2);
});

for (const scope of ["same page", "later page"]) {
  test(`duplicate transcript ID on ${scope} rejects`, async () => {
    const entries = scope === "same page"
      ? [page([record(0), record(0, "changed text with the same ID")], 2)]
      : [page([record(0)], 2, "page-2"), page([record(0, "changed text with the same ID")], 2)];
    const {fetchPage} = sequence(entries);
    await assert.rejects(readSnapshot(fetchPage, SOURCE), /分页不完整/);
  });
}

test("a missing items array rejects as an invalid page", async () => {
  const {fetchPage} = sequence([{total: 1, next_cursor: null, revision: "r1", snapshot: SNAPSHOT}]);
  await assert.rejects(readSnapshot(fetchPage, SOURCE), /格式异常/);
});

test("fewer records than total never commits a partial document", async () => {
  const {fetchPage, calls} = sequence([page([record(0)], 2), page([record(0)], 2)]);
  await assert.rejects(readSnapshot(fetchPage, SOURCE), /尚未读取完整/);
  assert.equal(calls.length, 2);
});

test("an empty intermediate page with a next cursor rejects", async () => {
  const {fetchPage} = sequence([page([record(0)], 2, "page-2"), page([], 2, "page-3")]);
  await assert.rejects(readSnapshot(fetchPage, SOURCE), /分页不完整/);
});

test("a revision change retries from a new snapshot without mixing old paragraphs", async () => {
  const old = record(0, "旧快照内容，不应进入结果");
  const fresh = [record(1, "新快照第一段"), record(2, "新快照第二段")];
  const {fetchPage, calls} = sequence([
    page([old], 2, "old-next", "old-revision"),
    page([record(99)], 2, null, "changed-revision"),
    page([fresh[0]], 2, "fresh-next", "new-revision", SNAPSHOT + 1),
    page([fresh[1]], 2, null, "new-revision", SNAPSHOT + 1),
  ]);
  const result = await readSnapshot(fetchPage, SOURCE);
  assert.deepEqual(result.items, fresh);
  assert.equal(result.revision, "new-revision");
  assert.equal(result.snapshot, SNAPSHOT + 1);
  assert.deepEqual(calls, [
    {source: SOURCE, cursor: null}, {source: SOURCE, cursor: "old-next"},
    {source: SOURCE, cursor: null}, {source: SOURCE, cursor: "fresh-next"},
  ]);
  assert.ok(!plainText(result.items).includes(old.text));
});

test("revision changes in both attempts reject instead of returning either partial snapshot", async () => {
  const {fetchPage, calls} = sequence([
    page([record(0)], 2, "first-next", "r1"), page([record(1)], 2, null, "r2"),
    page([record(2)], 2, "second-next", "r3", SNAPSHOT + 1),
    page([record(3)], 2, null, "r4", SNAPSHOT + 1),
  ]);
  await assert.rejects(readSnapshot(fetchPage, SOURCE), /尚未读取完整/);
  assert.equal(calls.length, 4);
  assert.ok(calls.every(query => query.source === SOURCE));
});

test("same known revision returns null without requesting remaining pages", async () => {
  const {fetchPage, calls} = sequence([page([record(0)], 2, "unused-next", "unchanged")]);
  assert.equal(await readSnapshot(fetchPage, SOURCE, "unchanged"), null);
  assert.deepEqual(calls, [{source: SOURCE, cursor: null}]);
});

test("empty full document is a successful snapshot and copies as empty text", async () => {
  const {fetchPage} = sequence([page([], 0)]);
  const result = await readSnapshot(fetchPage, null);
  assert.deepEqual(result.items, []);
  assert.equal(plainText(result.items), "");
});

test("speaker paragraphs preserve every whitespace and newline in authoritative text", () => {
  const text = " \t第一句。\n\n第二句！\r\n 第三句？\u00a0 \n";
  const result = paragraphs({text, segments: [
    {text: "第一句。", speaker_labels: ["我"]},
    {text: " 第二句！ ", speaker_labels: ["我"]},
    {text: "第三句？", speaker_labels: ["说话人 2", "我"]},
  ]});
  assert.equal(result.map(part => part.text).join(""), text);
  assert.deepEqual(result.map(part => part.label), ["我", "混合发言（说话人 2、我）"]);
});

test("repeated phrases stay in order and unlabeled turns retain text", () => {
  const text = "你好。\n你好。\t结束。\n";
  const result = paragraphs({text, segments: [
    {text: "你好。", speaker_labels: ["我"]},
    {text: "你好。", speaker_labels: ["说话人 2"]},
    {text: "结束。"},
  ]});
  assert.equal(result.map(part => part.text).join(""), text);
  assert.deepEqual(result.map(part => part.label), ["我", "说话人 2", "未区分"]);
});

for (const missing of [0, 1, 2]) {
  test(`incomplete segments missing turn ${missing + 1} fall back to the original full text`, () => {
    const text = "第一句。\n第二句。\n第三句。\n";
    const segments = ["第一句。", "第二句。", "第三句。"]
      .filter((_, index) => index !== missing)
      .map(part => ({text: part, speaker_labels: ["我"]}));
    assert.deepEqual(paragraphs({text, segments}), [{text, label: ""}]);
  });
}

test("segment wording or punctuation mismatch falls back without losing authoritative text", () => {
  const text = " 原文措辞。\n完整保留！ \n";
  assert.deepEqual(paragraphs({text, segments: [
    {text: "原文措词。", speaker_labels: ["我"]},
    {text: "完整保留！", speaker_labels: ["说话人 2"]},
  ]}), [{text, label: ""}]);
});

test("unannotated or absent segments use the original full text", () => {
  const text = " \n原文中的全部内容。\n其他段落。\t";
  for (const segments of [undefined, [], [{text: "原文中的全部内容。"}]]) {
    assert.deepEqual(paragraphs({text, segments}), [{text, label: ""}]);
  }
});

test("analyzed transcripts with absent or misaligned segments keep full text without guessing identities", () => {
  const text = " 全部原文。\n第二句。\t";
  for (const segments of [[], [{text: "不匹配的文字", speaker_labels: ["我"]}]]) {
    assert.deepEqual(paragraphs({text, speaker_count: 2, segments}),
      [{text, label: "已区分 2 位说话人 · 逐句归属未对齐"}]);
  }
  assert.deepEqual(paragraphs({text, speaker_count: 0, segments: []}),
    [{text, label: "未检测到说话人"}]);
});

test("plainText keeps long text and every paragraph rather than reconstructing from segments", () => {
  const text = `  开头\r\n\n${"长文字。 ".repeat(10000)}\n\t末段原文\n`;
  const items = [record(0, "前一条"), {...record(1, text), segments: [{text: "不完整摘要"}]}, record(2, "最后一条")];
  const result = plainText(items);
  assert.ok(result.includes(text));
  assert.ok(result.indexOf("前一条") < result.indexOf(text));
  assert.ok(result.indexOf(text) < result.indexOf("最后一条"));
  assert.ok(result.endsWith("最后一条"));
  assert.ok(!result.includes("不完整摘要"));
});
