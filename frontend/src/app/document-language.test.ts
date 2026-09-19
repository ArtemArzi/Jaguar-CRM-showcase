/// <reference types="node" />

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import ts from "typescript";
import { beforeAll, describe, expect, it } from "vitest";

const frontendRoot = process.cwd();
const sourceDocumentHtml = readFileSync(resolve(frontendRoot, "index.html"), "utf8");
const viteConfigSource = readFileSync(resolve(frontendRoot, "vite.config.ts"), "utf8");

function propertyValue(
  objectLiteral: ts.ObjectLiteralExpression,
  propertyName: string,
): ts.Expression | undefined {
  const property = objectLiteral.properties.find(
    (candidate): candidate is ts.PropertyAssignment =>
      ts.isPropertyAssignment(candidate) &&
      (ts.isIdentifier(candidate.name) || ts.isStringLiteral(candidate.name)) &&
      candidate.name.text === propertyName,
  );

  return property?.initializer;
}

function pwaManifestConfig(): ts.ObjectLiteralExpression | undefined {
  const sourceFile = ts.createSourceFile(
    "vite.config.ts",
    viteConfigSource,
    ts.ScriptTarget.Latest,
    true,
  );
  let manifest: ts.ObjectLiteralExpression | undefined;

  function visit(node: ts.Node): void {
    if (manifest) return;

    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === "VitePWA"
    ) {
      const [pwaOptions] = node.arguments;
      if (pwaOptions && ts.isObjectLiteralExpression(pwaOptions)) {
        const candidate = propertyValue(pwaOptions, "manifest");
        if (candidate && ts.isObjectLiteralExpression(candidate)) {
          manifest = candidate;
          return;
        }
      }
    }

    ts.forEachChild(node, visit);
  }

  ts.forEachChild(sourceFile, visit);
  return manifest;
}

describe("PWA document language", () => {
  it("declares Russian content and opts out of automatic browser translation", () => {
    const sourceDocument = new DOMParser().parseFromString(sourceDocumentHtml, "text/html");

    expect(sourceDocument.documentElement.lang).toBe("ru");
    expect(sourceDocument.documentElement.getAttribute("translate")).toBe("no");
    expect(sourceDocument.head.querySelector('meta[name="google"]')?.getAttribute("content")).toBe(
      "notranslate",
    );
  });
});

describe("Vite PWA manifest language", () => {
  it("configures Russian as the localized install metadata language", () => {
    const manifest = pwaManifestConfig();
    const language = manifest ? propertyValue(manifest, "lang") : undefined;

    expect(manifest).toBeDefined();
    expect(language && ts.isStringLiteral(language) ? language.text : undefined).toBe("ru");
  });
});

describe("built PWA language metadata", () => {
  beforeAll(
    () => {
      execFileSync(process.platform === "win32" ? "npm.cmd" : "npm", ["run", "build"], {
        cwd: frontendRoot,
        stdio: "inherit",
      });
    },
    30_000,
  );

  it("preserves the document language and translation opt-out in dist/index.html", () => {
    const builtDocumentHtml = readFileSync(resolve(frontendRoot, "dist/index.html"), "utf8");
    const builtDocument = new DOMParser().parseFromString(builtDocumentHtml, "text/html");

    expect(builtDocument.documentElement.lang).toBe("ru");
    expect(builtDocument.documentElement.getAttribute("translate")).toBe("no");
    expect(builtDocument.head.querySelector('meta[name="google"]')?.getAttribute("content")).toBe(
      "notranslate",
    );
  });

  it("emits Russian localized install metadata in dist/manifest.webmanifest", () => {
    const builtManifest = JSON.parse(
      readFileSync(resolve(frontendRoot, "dist/manifest.webmanifest"), "utf8"),
    ) as { lang?: unknown };

    expect(builtManifest.lang).toBe("ru");
  });
});
