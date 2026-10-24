"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const {spawnSync} = require("node:child_process");
const {installationRequest, shellQuote} = require("../audio_agent/web_assets/agent-setup.js");

const data = {transport:"stdio", project_dir:"/tmp/项目 空格", python_path:"/tmp/项目 空格/.venv/bin/python", data_dir:"/tmp/项目 空格/.data", follows_storage_setting:true};
function installLines(text) { return text.split("\n").filter(line => line.includes(" --client ")); }
function split(text) {
  const result = spawnSync("python3", ["-c", "import sys,json,shlex;print(json.dumps(shlex.split(sys.stdin.read())))"], {input:text, encoding:"utf8"});
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

test("project request preserves caller workspace and follows saved library", () => {
  const lines = installLines(installationRequest(data, "codex", "project"));
  assert.equal(lines.length, 2);
  const args = split(lines[1]);
  assert.deepEqual(args, [data.python_path, data.project_dir+"/scripts/install-agent-integration", "--client", "codex", "--scope", "project", "--workspace", "$audio_agent_workspace"]);
  assert.deepEqual(split(lines[0]), [...args, "--dry-run"]);
});

test("user request omits workspace and pins only a mismatching Web library", () => {
  const pinned = {...data, follows_storage_setting:false, data_dir:"/tmp/另一个 库"};
  const args = split(installLines(installationRequest(pinned, "claude", "user"))[1]);
  assert.deepEqual(args, [data.python_path, data.project_dir+"/scripts/install-agent-integration", "--client", "claude", "--scope", "user", "--data-dir", pinned.data_dir]);
});

test("quotes and shell metacharacters stay literal in generated commands", () => {
  const path = "/tmp/中文 '$(touch SHOULD_NOT_EXIST);`echo bad` \\\"";
  const tricky = {...data, project_dir:path, python_path:path+"/.venv/bin/python", data_dir:path+"/.data", follows_storage_setting:false};
  const args = split(installLines(installationRequest(tricky,"codex"))[1]);
  assert.equal(args[0], tricky.python_path);
  assert.equal(args[1], path+"/scripts/install-agent-integration");
  assert.equal(args.at(-1), tricky.data_dir);
  assert.deepEqual(split(shellQuote(path)), [path]);
});

test("incomplete context cannot produce a plausible installer request", () => {
  assert.throws(() => installationRequest({...data, project_dir:undefined}, "codex"));
  assert.throws(() => installationRequest({...data, follows_storage_setting:undefined}, "codex"));
  assert.throws(() => installationRequest(data, "unknown"));
  assert.throws(() => installationRequest(data, "codex", "machine"));
  assert.throws(() => shellQuote("/tmp/path\n--bad"));
});
