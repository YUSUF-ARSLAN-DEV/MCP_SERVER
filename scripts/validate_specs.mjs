import { readFileSync, readdirSync } from 'fs';

const requested = process.argv.slice(2);
const files = requested.length ? requested.flatMap(file => file.includes('*') ? readdirSync('tests').filter(name => name.endsWith('.spec.ts')).map(name => `tests/${name}`) : [file]) : readdirSync('tests').filter(name => name.endsWith('.spec.ts')).map(name => `tests/${name}`);
const behaviorWords = /\b(allow|navigat\w*|switch\w*|select\w*|submit\w*|open\w*|interact\w*|search\w*|change\w*|filter\w*|validat\w*|toggle\w*|functional)\b/i;
const action = /\.(click|selectOption|fill|check|uncheck|press|setInputFiles|hover)\s*\(/;
const evidence = /actionEvidence\s*\(/;
const meaningful = /toHaveURL|toHaveValue|toHaveText|toContainText|toBeVisible|toHaveCount|toBeEnabled|toBeChecked|toBeGreaterThan/;
const validActionCall = /actionEvidence\s*\(\s*page\s*,\s*testInfo\s*,/;
const validObservationCall = /observationEvidence\s*\(\s*page\s*,\s*testInfo\s*,/;
const genericTitle = /^(should\s+)?(work|works|display correctly|displays correctly|functional navigation|selects a country and channel)$/i;
const failures = [];

for (const file of files) {
  const code = readFileSync(file, 'utf8');
  if (/:has-text\s*\(|locator\(\s*['"]text=|getByText\s*\(/.test(code)) failures.push(`${file}: unstable text selector`);
  if (/actionEvidence\s*\(/.test(code) && !validActionCall.test(code)) failures.push(`${file}: actionEvidence must be called as actionEvidence(page, testInfo, label, action, verify)`);
  if (/observationEvidence\s*\(/.test(code) && !validObservationCall.test(code)) failures.push(`${file}: observationEvidence must be called as observationEvidence(page, testInfo, label, verify)`);
  if (!/test\.describe\s*\(\s*['"]https?:\/\//.test(code)) failures.push(`${file}: missing full URL in test.describe`);
  for (const block of code.split(/(?=\btest\s*\()/).filter(Boolean)) {
    const title = block.match(/test\s*\(\s*['"]([^'"]+)/)?.[1] || '';
    const attachmentOnly = (block.match(/toBeAttached\s*\(/g) || []).length > 0 && !/toBeVisible|toHaveValue|toHaveURL|toHaveText|toHaveCount|toBeEnabled|toBeChecked/.test(block);
    if (behaviorWords.test(title) && action.test(block) && !evidence.test(block)) failures.push(`${file}: "${title}" performs an action without actionEvidence`);
    if (behaviorWords.test(title) && action.test(block) && !meaningful.test(block) && !/NOT TESTABLE/i.test(block)) failures.push(`${file}: "${title}" has no meaningful postcondition`);
    if (genericTitle.test(title.trim())) failures.push(`${file}: generic test title "${title}"`);
    if (behaviorWords.test(title) && attachmentOnly && !action.test(block) && !/NOT TESTABLE/i.test(block)) failures.push(`${file}: "${title}" claims behavior but only checks attachment`);
  }
}

if (failures.length) { console.error(failures.map(x => `  - ${x}`).join('\n')); process.exit(1); }
console.log(`Validated ${files.length} spec file(s).`);
