import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";

const pageUrl = new URL("../app/page.tsx", import.meta.url);
const publicUrl = new URL("../public/", import.meta.url);

test("renders a visible security-reviewed badge and clear non-accredited disclaimer", async () => {
  const page = await readFile(pageUrl, "utf8");

  assert.match(page, /SECURITY REVIEWED/);
  assert.match(page, /3 AI TECHNICAL ATTESTATIONS/);
  assert.match(page, /Valutazioni tecniche informative e non accreditate/);
});

test("provides an accessible in-page viewer for Claude, ChatGPT and DeepSeek attestations", async () => {
  const page = await readFile(pageUrl, "utf8");

  assert.match(page, /securityAttestationsOpen/);
  assert.match(page, /role="dialog"/);
  assert.match(page, /aria-modal="true"/);
  assert.match(page, /Claude/);
  assert.match(page, /ChatGPT/);
  assert.match(page, /DeepSeek/);
  assert.match(page, /setSecurityAttestationsOpen\(true\)/);
  assert.match(page, /setSecurityAttestationsOpen\(false\)/);
});

test("publishes the three source attestations under stable public paths", async () => {
  const files = (await readdir(publicUrl, { recursive: true })).map((value) => String(value).replaceAll("\\", "/"));

  assert.ok(files.includes("security-attestations/claude/traxion-security-attestation-claude-2026-09-03.png"));
  assert.ok(files.includes("security-attestations/chatgpt/traxion-security-attestation-chatgpt-2026-09-03.png"));
  assert.ok(files.includes("security-attestations/deepseek/traxion-security-attestation-deepseek-2026-09-03.txt"));
  assert.ok(files.includes("security-attestations/README.md"));
});
