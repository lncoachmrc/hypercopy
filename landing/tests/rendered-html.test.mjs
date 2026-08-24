import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";

const developmentPreviewMeta =
  /<meta(?=[^>]*\bname=["']codex-preview["'])(?=[^>]*\bcontent=["']development["'])[^>]*>/i;

test("renders development preview metadata", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  const response = await worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );

  assert.equal(response.status, 200);
  assert.match(
    response.headers.get("content-type") ?? "",
    /^text\/html\b/i,
  );
  assert.match(await response.text(), developmentPreviewMeta);
});

async function renderHome() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("traxion-qa", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const response = await worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
  assert.equal(response.status, 200);
  return response.text();
}

test("renders the verified TRAXION commercial experience", async () => {
  const html = await renderHome();

  assert.equal((html.match(/<h1\b/gi) ?? []).length, 1);
  assert.match(html, /Intelligenza ibrida/);
  assert.match(html, /Esecuzione deterministica/);
  assert.match(html, /14 giorni/);
  assert.match(html, /2\.500 USD/);
  assert.match(html, /5\.000 USD/);
  assert.match(html, /10\.000 USD/);
  assert.match(html, /API Wallet Private Key/);
  assert.match(html, /modalità SHADOW/i);
  assert.match(html, /FAQPage/);

  assert.match(html, /https:\/\/frontend-staging-9498\.up\.railway\.app\/login/);
  assert.match(html, /href="https:\/\/app\.hyperliquid\.xyz\/join\/DIGITALEMPOWER"[^>]*rel="sponsored noopener noreferrer"/);
  assert.match(html, /href="https:\/\/app\.hyperliquid\.xyz\/API"[^>]*rel="noopener noreferrer"/);

  assert.doesNotMatch(html, /guadagn(?:o|i) garantiti|profitto garantito|senza rischio|l['’]AI prevede il mercato/i);
});

test("keeps all localized whitepapers raster-only and complete", async () => {
  const publicFiles = await readdir(new URL("../public/", import.meta.url), { recursive: true });
  const lower = publicFiles.map((name) => String(name).toLowerCase());

  assert.equal(
    lower.filter((name) => name.endsWith(".pdf") || name.endsWith(".docx") || name.endsWith(".md")).length,
    0,
  );
  assert.equal(lower.filter((name) => /^whitepaper\/trx-wp-0[1-6]\.webp$/.test(name)).length, 6);
  assert.equal(lower.filter((name) => /^whitepaper\/en\/trx-wp-en-0[1-6]\.webp$/.test(name)).length, 6);
  assert.equal(lower.filter((name) => /^whitepaper\/es\/trx-wp-es-0[1-6]\.webp$/.test(name)).length, 6);

  const localeResolver = await readFile(new URL("../app/whitepaper-locale.ts", import.meta.url), "utf8");
  assert.match(localeResolver, /whitepaper\/en\/trx-wp-en-/);
  assert.match(localeResolver, /whitepaper\/es\/trx-wp-es-/);
  assert.match(localeResolver, /whitepaper\/trx-wp-/);

  const localizer = await readFile(new URL("../app/WhitepaperAssetLocalizer.tsx", import.meta.url), "utf8");
  assert.match(localizer, /localizedWhitepaperAsset\(detectLanguage\(\),page\)/);

  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  const robots = await readFile(new URL("../public/robots.txt", import.meta.url), "utf8");
  const headers = await readFile(new URL("../public/_headers", import.meta.url), "utf8");

  assert.match(css, /user-select:\s*none/);
  assert.match(css, /@media print[\s\S]*\.whitepaper-viewer\s*\{\s*display:\s*none !important/);
  assert.match(robots, /Disallow:\s*\/whitepaper\//);
  assert.match(headers, /\/whitepaper\/\*[\s\S]*X-Robots-Tag:\s*noindex, nofollow, noarchive/);
});

test("centralizes external product URLs", async () => {
  const config = await readFile(new URL("../app/config.ts", import.meta.url), "utf8");
  const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");

  assert.match(config, /export const TRAXION_APP_URL/);
  assert.match(config, /export const HYPERLIQUID_REFERRAL_URL/);
  assert.match(config, /export const HYPERLIQUID_API_WALLET_URL/);
  assert.doesNotMatch(page, /frontend-staging-9498\.up\.railway\.app/);
  assert.match(page, /onContextMenu=\{\(event\) => event\.preventDefault\(\)\}/);
  assert.match(page, /onDragStart=\{\(event\) => event\.preventDefault\(\)\}/);
  assert.match(page, /\["s", "p", "c", "a"\]\.includes\(key\)/);
  assert.match(page, /event\.key === "ArrowLeft"/);
  assert.match(page, /event\.key === "ArrowRight"/);

  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(css, /@media \(max-width: 820px\)/);
  assert.match(css, /@media \(max-width: 580px\)/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
});
