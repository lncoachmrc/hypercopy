import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const productionApi = "https://api.traxion.lucianonovello.com";
const legacyStagingApi = /https:\/\/api-staging-[^"'\s]+\.up\.railway\.app/;

test("landing performance targets the production TRAXION API", async () => {
  const chart = await readFile(
    new URL("../app/MasterPerformancePortal.tsx", import.meta.url),
    "utf8",
  );
  const buildScript = await readFile(
    new URL("../scripts/build-pages.sh", import.meta.url),
    "utf8",
  );

  assert.match(chart, new RegExp(productionApi.replaceAll(".", "\\.")));
  assert.doesNotMatch(chart, legacyStagingApi);
  assert.match(
    buildScript,
    /NEXT_PUBLIC_TRAXION_API_URL=https:\/\/api\.traxion\.lucianonovello\.com/,
  );
});
