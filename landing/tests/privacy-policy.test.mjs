import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function readSource(path) {
  try {
    return await readFile(new URL(path, import.meta.url), "utf8");
  } catch {
    return "";
  }
}

test("publishes the privacy policy at /privacy/ and links it from the landing", async () => {
  const config = await readSource("../app/config.ts");
  const landing = await readSource("../app/page.tsx");
  const privacy = await readSource("../app/privacy/page.tsx");

  assert.match(config, /PRIVACY_POLICY_URL\s*=\s*["']\/privacy\/["']/);
  assert.match(landing, /href=\{PRIVACY_POLICY_URL\}/);
  assert.match(landing, />Privacy Policy</);
  assert.match(privacy, /Privacy Policy/);
  assert.match(privacy, /Dati identificativi completi del titolare in corso di completamento/);
  assert.doesNotMatch(privacy, /\[NOME|\[NIF|\[EMAIL PRIVACY|\[DA INSERIRE/);
});
