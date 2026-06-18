#!/usr/bin/env node
/**
 * Deep-merge a LIFT config fragment into the config produced by plugin install.
 * Usage: node merge-openclaw-config.mjs <target.json> <template.json>
 */
import { readFileSync, writeFileSync } from "node:fs";

const [targetPath, templatePath] = process.argv.slice(2);
if (!targetPath || !templatePath) {
  console.error("Usage: merge-openclaw-config.mjs <target> <template>");
  process.exit(1);
}

function deepMerge(base, patch) {
  if (patch === null || typeof patch !== "object" || Array.isArray(patch)) {
    return patch;
  }
  const out = { ...(base && typeof base === "object" ? base : {}) };
  for (const [k, v] of Object.entries(patch)) {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      out[k] = deepMerge(out[k], v);
    } else {
      out[k] = v;
    }
  }
  return out;
}

const target = JSON.parse(readFileSync(targetPath, "utf8"));
const template = JSON.parse(readFileSync(templatePath, "utf8"));
const merged = deepMerge(target, template);
writeFileSync(targetPath, JSON.stringify(merged, null, 2) + "\n", "utf8");
console.log("Merged config ->", targetPath);
