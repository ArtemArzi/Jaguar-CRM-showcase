import { readdirSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

const distDir = fileURLToPath(new URL("../dist/assets/", import.meta.url));
const budgets = {
  // The signed v2 group-offer picker/review/payment flow adds about 15 KB of
  // emitted JS. Keep only a narrow allowance above its measured release
  // baseline so subsequent aggregate growth still fails closed in CI.
  jsRawBytes: 1_270_000,
  jsGzipBytes: 405_000,
  cssRawBytes: 180_000,
  cssGzipBytes: 45_000,
};

function walk(dir) {
  const files = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const fullPath = join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...walk(fullPath));
    } else {
      files.push(fullPath);
    }
  }
  return files;
}

function totalFor(files, suffix) {
  return files
    .filter((file) => file.endsWith(suffix))
    .reduce(
      (acc, file) => {
        const raw = statSync(file).size;
        const gzip = gzipSync(readFileSync(file)).length;
        return { raw: acc.raw + raw, gzip: acc.gzip + gzip };
      },
      { raw: 0, gzip: 0 },
    );
}

const files = walk(distDir);
const js = totalFor(files, ".js");
const css = totalFor(files, ".css");
const metrics = {
  jsRawBytes: js.raw,
  jsGzipBytes: js.gzip,
  cssRawBytes: css.raw,
  cssGzipBytes: css.gzip,
};

console.table(metrics);

let failed = false;
for (const [metric, value] of Object.entries(metrics)) {
  if (value > budgets[metric]) {
    console.error(
      `Bundle budget exceeded: ${metric}=${value} > ${budgets[metric]}`,
    );
    failed = true;
  }
}

if (failed) {
  process.exitCode = 1;
}
