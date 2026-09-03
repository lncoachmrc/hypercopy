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

test("publishes terms at /terms/ and links them from the landing footer", async () => {
  const config = await readSource("../app/config.ts");
  const landing = await readSource("../app/page.tsx");
  const terms = await readSource("../app/terms/page.tsx");

  assert.match(config, /TERMS_URL\s*=\s*["']\/terms\/["']/);
  assert.match(landing, /href=\{TERMS_URL\}/);
  assert.match(landing, />Termini e Condizioni</);
  assert.match(terms, /Termini e Condizioni/);
  assert.match(terms, /14 giorni/);
  assert.match(terms, /consumatore/i);
  assert.match(terms, /Hyperliquid/);
  assert.match(terms, /Stripe/);
  assert.match(terms, /rischio/i);
  assert.match(terms, /Dati identificativi completi del fornitore in corso di completamento/);
  assert.doesNotMatch(terms, /\[NOME|\[NIF|\[EMAIL|\[DA INSERIRE/);
});
