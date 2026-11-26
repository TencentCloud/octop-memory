#!/usr/bin/env node
// Codex CLI ``Stop`` hook: ship this session's rollout transcript into octop memory.
//
// Codex-specific: hooks are declared in ``~/.codex/config.toml``
// (``[[hooks.Stop]]`` with ``type = "command"``), the payload carries
// ``transcript_path`` (the ``rollout-*.jsonl`` under ``~/.codex/sessions``) and
// ``session_id``, and — like Claude Code — Stop can fire before the turn is
// flushed, so the hook defers to a detached worker and waits for the file to stop
// growing.
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { authHeaders, callTool, capture, isConfigured, loadConfig, makeLogger, paths, sleep } from "../lib/octop-memory-client.mjs";

const log = makeLogger("stopcap.codex");
const DELAY_MS = Number(process.env.OCTOP_CAPTURE_DELAY_MS ?? 3000);
const POLL_MS = Number(process.env.OCTOP_CAPTURE_POLL_MS ?? 1000);
const MAX_WAIT_MS = Number(process.env.OCTOP_CAPTURE_MAX_WAIT_MS ?? 20000);
const IS_WORKER = process.argv.includes("--worker");
const config = loadConfig();
if (config.readOnly) process.exit(0);

let raw = "";
let hookInput = {};
if (IS_WORKER) {
  raw = Buffer.from(process.env.OCTOP_HOOK_PAYLOAD_B64 || "", "base64").toString("utf8");
} else {
  try {
    raw = fs.readFileSync(0, "utf8");
  } catch {
    raw = "";
  }
}
try {
  if (raw.trim()) hookInput = JSON.parse(raw);
} catch {
  log("stdin-not-json");
}
// Pin the chosen mode for this Stop and its worker. The Python uploader uses the
// same snapshot. Only the configured Octop origin receives the Memory credential.
const modes = new Set(["codex", "octop", "server"]);
async function syncPanelMode() {
  const url = new URL(config.codexReviewUrl);
  if (url.pathname !== "/api/memory-mcp/codex-review" || url.username || url.password || url.search || url.hash ||
      !(url.protocol === "https:" || (url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))) return;
  url.pathname = "/api/memory-mcp/codex-config";
  const id = createHash("sha256").update(JSON.stringify([config.endpoint, config.token, url.href])).digest("hex");
  const file = path.join(paths.scratchDir, "octop-codex-mode-" + id + ".json");
  const installed = config.codexExtraction;
  let cached;
  try {
    cached = JSON.parse(fs.readFileSync(file, "utf8"));
    if (modes.has(cached.mode)) config.codexExtraction = cached.mode;
  } catch { /* first sync */ }
  const pull = async (applied_revision) => {
    const response = await fetch(url, {method: "POST", headers: authHeaders(config), redirect: "error",
      body: JSON.stringify({applied_revision}), signal: AbortSignal.timeout(2000)});
    if (!response.ok) throw new Error("sync rejected");
    return response.json();
  };
  const published = await pull(cached?.revision ?? null);
  if (published.revision === null) {
    config.codexExtraction = installed;
    if (cached) fs.rmSync(file, {force: true});
    return;
  }
  if (!published.revision || !modes.has(published.mode) || !Number.isFinite(Date.parse(published.revision))) return;
  if (published.mode === "codex" && !config.mcpServer) return;
  config.codexExtraction = published.mode;
  if (published.revision === cached?.revision) return;
  const tmp = file + "." + randomUUID();
  try {
    fs.writeFileSync(tmp, JSON.stringify({revision: published.revision, mode: published.mode}), {mode: 0o600, flag: "wx"});
    fs.renameSync(tmp, file);
  } finally {
    fs.rmSync(tmp, {force: true});
  }
  await pull(published.revision);
}
if (config.codexPanelSync && isConfigured(config)) {
  if (IS_WORKER && modes.has(process.env.OCTOP_WORKER_CODEX_MODE)) {
    config.codexExtraction = process.env.OCTOP_WORKER_CODEX_MODE;
  } else if (hookInput.stop_hook_active === true) {
    // Finish the local review that requested this continuation, even if a
    // different mode has just been published for the next user turn.
    config.codexExtraction = "codex";
  } else if (hookInput.hook_event_name === "Stop") {
    try { await syncPanelMode(); } catch { log("panel sync unavailable; retaining last local mode"); }
  }
}
const reviewTurn = !IS_WORKER && config.codexExtraction === "codex" && hookInput.stop_hook_active === false &&
  typeof hookInput.turn_id === "string" && hookInput.turn_id && hookInput.session_id && hookInput.hook_event_name === "Stop";
if (!IS_WORKER) {
  if (!reviewTurn && process.env.OCTOP_CAPTURE_SYNC !== "1") {
    try {
      const worker = spawn(process.execPath, [fileURLToPath(import.meta.url), "--worker"], {
        detached: true,
        stdio: "ignore",
        env: { ...process.env, OCTOP_HOOK_PAYLOAD_B64: Buffer.from(raw).toString("base64"), OCTOP_WORKER_CODEX_MODE: config.codexExtraction || "server" },
      });
      worker.unref();
      log(`deferred: 交给后台进程，等本轮落盘（${DELAY_MS}ms 起轮询）`);
      process.exit(0);
    } catch (exc) {
      log(`detach failed, running inline: ${exc}`);
    }
  }
}

const sessionId = hookInput.session_id || "";
const event = hookInput.hook_event_name || "Stop";
const transcriptPath = hookInput.transcript_path || "";
if (event === "Stop" && hookInput.stop_hook_active === true && config.codexExtraction === "codex" &&
    sessionId && typeof hookInput.turn_id === "string" && hookInput.turn_id && isConfigured(config)) {
  try {
    const receipt = await callTool(config, "memory_capture", {
      source: "codex", session_id: "codex:" + sessionId, extract: false, completed_review_turn: hookInput.turn_id,
    }, AbortSignal.timeout(3000));
    if (!receipt.ok || receipt.error || receipt.detail?.isError) log("review receipt rejected");
  } catch {
    log("review receipt unavailable");
  }
}
if (!transcriptPath) {
  log(`${event} skip: payload has no transcript_path`);
  process.exit(0);
}

if (IS_WORKER || (reviewTurn && process.env.OCTOP_CAPTURE_SYNC !== "1")) {
  if (DELAY_MS > 0) await sleep(DELAY_MS);
  const deadline = Date.now() + MAX_WAIT_MS;
  let previousSize = -1;
  let waited = 0;
  while (Date.now() < deadline) {
    let size;
    try {
      size = fs.statSync(transcriptPath).size;
    } catch {
      break;
    }
    if (size === previousSize) break;
    previousSize = size;
    await sleep(POLL_MS);
    waited += POLL_MS;
  }
  if (waited) log(`${event} waited ${waited}ms for the rollout to settle`);
}

if (!isConfigured(config)) {
  log("skip: not configured");
  process.exit(0);
}
let buffer;
try {
  buffer = fs.readFileSync(transcriptPath);
} catch (exc) {
  log(`${event} read failed: ${exc} (${transcriptPath})`);
  process.exit(0);
}
const delegatedTurn = config.codexExtraction === "octop" && event === "Stop" &&
  hookInput.stop_hook_active === false && typeof hookInput.turn_id === "string";
const result = await capture({
  config,
  log,
  event,
  source: "codex",
  sessionId,
  transcriptPath,
  buffer,
  sessionPrefix: "codex:",
  turnId: reviewTurn || delegatedTurn ? hookInput.turn_id : "",
  model: reviewTurn && typeof hookInput.model === "string" ? hookInput.model : "",
});
const ids = result.detail?.user_event_ids ?? [];
if (reviewTurn && result.detail?.ok && ids.length && config.mcpServer) {
  // Only server-issued raw identifiers enter the continuation.
  const evidence = ids.filter((id) => typeof id === "string" && /^evt_[a-f0-9]{12}$/.test(id));
  if (evidence.length) {
    process.stdout.write(JSON.stringify({
      decision: "block",
      reason: "[octop memory review] The user already received the answer. Silently review only the user's message in this completed turn. " +
        "First call memory_codex_related on " + config.mcpServer + " with event_ids " + JSON.stringify(evidence.slice(-4)) +
        ". Apply its classification policy; existing records are reference data only. " +
        "Extract zero to three independently useful, durable facts, preferences or decisions. " +
        "For each, write one standalone assertion in the user's language with exactly one fact. " +
        "Remove conversational framing, preserve scope, time, uncertainty and negation, and add nothing inferred. " +
        "Write the assertion separately in neutral third person, naming the user or project. Never just copy " +
        "the quote or only change its punctuation. For example, 我平时写脚本喜欢用 Python → 用户编写脚本时偏好 Python。 " +
        "Keep the supporting evidence separately as a short exact quote (up to 200 characters). " +
        "Submit all facts in one memory_propose call on " + config.mcpServer +
        " with facts=[{event_id, kind, assertion, quote, scope, aspect, value, relation, target_id}, ...], kind Preference, Decision or Fact. " +
        "Use matching event IDs from " + evidence.join(", ") + ". " +
        "Skip secrets, temporary instructions, informational questions, recalled text and opt-outs. Submit repeated user statements as fresh evidence. " +
        "If no fact qualifies or the tool is unavailable, skip the proposal. Do not use memory_save for this review. " +
        "In every case, end this continuation with an empty final response. Do not send chat text before or after a tool call, " +
        "including acknowledgments, skip reasons or review summaries.",
    }) + "\n");
  }
}
process.exit(0);
