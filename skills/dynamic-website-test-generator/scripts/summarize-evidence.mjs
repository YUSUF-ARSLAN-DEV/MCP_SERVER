#!/usr/bin/env node
// summarize-evidence.mjs - turn Playwright's JSON results plus the evidence folder into one readable report.
//
// Usage: node summarize-evidence.mjs [--results evidence/results.json] [--evidence evidence] [--out evidence/REPORT.md]
// Reads local files only. Writes REPORT.md and evidence/index.json.
import fs from "node:fs";
import path from "node:path";

const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf(n); return i >= 0 && args[i + 1] ? args[i + 1] : d; };
const resultsFile = opt("--results", "evidence/results.json");
const evidenceRoot = opt("--evidence", "evidence");
const outFile = opt("--out", path.join(evidenceRoot, "REPORT.md"));

if (!fs.existsSync(resultsFile)) {
  console.error(`No results file at ${resultsFile}. Run: npx playwright test (the config writes it).`);
  process.exit(2);
}
const data = JSON.parse(fs.readFileSync(resultsFile, "utf8"));
const rows = [];
(function walk(suite, file) {
  for (const s of suite.suites ?? []) walk(s, s.file ?? file);
  for (const spec of suite.specs ?? []) {
    for (const t of spec.tests ?? []) {
      const last = t.results?.[t.results.length - 1];
      rows.push({
        file: spec.file ?? file,
        title: spec.title,
        status: t.status === "expected" ? "passed" : t.status === "skipped" ? "skipped" : "failed",
        ms: last?.duration ?? 0,
        error: last?.error?.message?.split("\n")[0]?.replace(/\u001b\[[0-9;]*m/g, "") ?? "",
        trace: (last?.attachments ?? []).find((a) => a.name === "trace")?.path,
      });
    }
  }
})({ suites: data.suites ?? [] }, "");

const safe = (s) => s.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^-|-$/g, "").slice(0, 100);
const shots = (title) => {
  const dirs = fs.existsSync(evidenceRoot) ? fs.readdirSync(evidenceRoot, { withFileTypes: true }).filter((d) => d.isDirectory() && !d.name.startsWith("_")) : [];
  const dir = dirs.find((d) => d.name.endsWith(safe(title)) || d.name.includes(safe(title)));
  return dir ? fs.readdirSync(path.join(evidenceRoot, dir.name)).filter((f) => f.endsWith(".png")).map((f) => path.posix.join(dir.name, f)) : [];
};

const passed = rows.filter((r) => r.status === "passed").length;
const failed = rows.filter((r) => r.status === "failed").length;
const skipped = rows.filter((r) => r.status === "skipped").length;
const index = rows.map((r) => ({ ...r, screenshots: shots(r.title) }));
fs.writeFileSync(path.join(evidenceRoot, "index.json"), JSON.stringify(index, null, 2));

let md = `# Test run summary\n\n${passed} passed, ${failed} failed, ${skipped} skipped (${rows.length} tests).\n\n`;
md += "| Test | Result | Screenshots |\n| --- | --- | --- |\n";
for (const r of index) md += `| ${r.title} | ${r.status}${r.error ? ` - ${r.error}` : ""} | ${r.screenshots.length} |\n`;
md += "\n## Evidence\n";
for (const r of index) {
  md += `\n### ${r.title} (${r.status})\n`;
  for (const s of r.screenshots) md += `- ${s}\n`;
  if (r.trace) md += `- trace: ${path.relative(process.cwd(), r.trace)} (open with \`npx playwright show-trace\`)\n`;
}
fs.writeFileSync(outFile, md);
console.log(`${passed} passed, ${failed} failed, ${skipped} skipped -> ${outFile}`);
process.exit(failed ? 1 : 0);
