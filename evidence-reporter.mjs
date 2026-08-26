import { mkdirSync, writeFileSync } from 'fs';
import { spawnSync } from 'child_process';
export default class EvidenceReporter {
  constructor(options = {}) { this.outputFile = options.outputFile || 'evidence.json'; this.results = []; }
  onTestEnd(test, result) { this.results.push({ title: test.title, titlePath: test.titlePath(), file: test.location?.file, status: result.status, durationMs: result.duration, error: result.error ? { message: result.error.message, stack: result.error.stack } : null, attachments: (result.attachments || []).filter(a => a.path && a.name !== 'screenshot' && /image\//i.test(a.contentType || '')).map(a => ({ name: a.name, contentType: a.contentType, path: a.path })) }); }
  async onEnd(fullResult) { mkdirSync('reports', { recursive: true }); writeFileSync(this.outputFile, JSON.stringify({ generatedAt: new Date().toISOString(), status: fullResult.status, results: this.results }, null, 2)); const r = spawnSync(process.env.PYTHON || 'python', ['scripts/generate_evidence_docx.py', this.outputFile, 'reports/test-evidence.docx'], { stdio: 'inherit' }); if (r.status === 0) console.log('Word evidence report: reports/test-evidence.docx'); else console.warn('Evidence JSON was written, but the Word report could not be generated.'); }
}
