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

test("publishes a Spanish consumer terms version and cross-links both languages", async () => {
  const termsIt = await readSource("../app/terms/page.tsx");
  const termsEs = await readSource("../app/terms/es/page.tsx");

  assert.match(termsIt, /href=["']\/terms\/es\/["']/);
  assert.match(termsIt, /Español/);
  assert.match(termsEs, /Términos y Condiciones/);
  assert.match(termsEs, /consumidor/i);
  assert.match(termsEs, /14 días/);
  assert.match(termsEs, /Hyperliquid/);
  assert.match(termsEs, /Stripe/);
  assert.match(termsEs, /href=["']\/terms\/["']/);
  assert.match(termsEs, /Italiano/);
  assert.match(termsEs, /Domicilio profesional/i);
  assert.doesNotMatch(termsEs, /\[NOMBRE|\[NIF|\[EMAIL|\[POR INSERTAR/);
});

test("includes the model withdrawal notice and form in Italian and Spanish", async () => {
  const termsIt = await readSource("../app/terms/page.tsx");
  const termsEs = await readSource("../app/terms/es/page.tsx");

  assert.match(termsIt, /Modulo tipo di recesso/);
  assert.match(termsIt, /Con la presente comunico\/comunichiamo il recesso/);
  assert.match(termsIt, /Nome del\/dei consumatore\/i/);
  assert.match(termsEs, /Modelo de formulario de desistimiento/);
  assert.match(termsEs, /Por la presente comunico\/comunicamos que desisto\/desistimos/);
  assert.match(termsEs, /Nombre del\/de los consumidor\/es/);
});

test("does not shift authenticated-activity proof onto the consumer", async () => {
  const termsIt = await readSource("../app/terms/page.tsx");
  const termsEs = await readSource("../app/terms/es/page.tsx");

  assert.doesNotMatch(termsIt, /salvo prova di compromissione o errore del servizio/i);
  assert.doesNotMatch(termsEs, /salvo prueba de compromiso o error del servicio/i);
});

test("makes each terms route authoritative for its legal locale", async () => {
  const controller = await readSource("../app/LanguageSelector.tsx");

  assert.match(controller, /function termsRouteLanguage\(pathname:string\)/);
  assert.match(controller, /if\(normalized===["']\/terms\/es["']\)return ["']es["']/);
  assert.match(controller, /if\(normalized===["']\/terms["']\)return ["']it["']/);
  assert.match(controller, /const routeLanguage=termsRouteLanguage\(window\.location\.pathname\)/);
  assert.match(controller, /persistLanguageSelection\(routeLanguage\)/);
  assert.match(controller, /document\.documentElement\.lang=htmlLocaleByLanguage\[routeLanguage\]/);
  assert.match(controller, /const translateCleanup=routeLanguage\?\(\)=>\{\}:initAutoTranslate\(\)/);
  assert.match(controller, /if\(!routeLanguage&&language===["']en["']\)/);
  assert.match(controller, /else if\(!routeLanguage&&language===["']es["']\)/);
});
