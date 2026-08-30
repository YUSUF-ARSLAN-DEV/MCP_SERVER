from __future__ import annotations
import argparse, json, logging, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
from .config import Settings
from .explorer import explore
from .generator import generate_spec
from .llm import ModelClient, diagnose_error
from .urls import read_urls

def name(url: str) -> str: return re.sub(r'[^a-z0-9]+', '-', url.lower()).strip('-')[:100]

def main() -> int:
    parser = argparse.ArgumentParser(description='Explore websites and generate validated Python Playwright smoke tests.')
    parser.add_argument('command', choices=['generate','explore','execute'], nargs='?', default='generate'); args = parser.parse_args()
    settings = Settings(); settings.prepare(); logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[logging.FileHandler(settings.artifacts_dir/'generation.log', encoding='utf-8'), logging.StreamHandler()]); log = logging.getLogger('pipeline')
    urls = read_urls(settings.urls_file)
    guide = (settings.root/'AI-TEST-GUIDE.md').read_text(encoding='utf-8') if (settings.root/'AI-TEST-GUIDE.md').exists() else ''
    persona = (settings.root/'persona.txt').read_text(encoding='utf-8') if (settings.root/'persona.txt').exists() else ''
    manifest = {'started_at': datetime.now(timezone.utc).isoformat(), 'urls': {}}; (settings.artifacts_dir/'run.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    if args.command == 'execute':
        result = subprocess.run([sys.executable, '-m', 'pytest', str(settings.tests_dir), '-q'], cwd=settings.root)
        return result.returncode
    client = ModelClient(settings, log)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.headless)
        for url in urls:
            log.info('Processing %s', url)
            try:
                context = browser.new_context(); page = context.new_page(); page.set_default_navigation_timeout(settings.navigation_timeout_ms); page.goto(url, wait_until='domcontentloaded'); inventory = explore(page, url)
                (settings.artifacts_dir/f'{name(url)}.inventory.json').write_text(json.dumps(inventory.__dict__, indent=2, ensure_ascii=False), encoding='utf-8')
                if args.command == 'generate':
                    output = settings.tests_dir/f'{name(url)}_test.py'; generate_spec(client, guide, persona, inventory, output); manifest['urls'][url] = {'status':'generated','spec':str(output)}
                else: manifest['urls'][url] = {'status':'explored'}
                context.close()
            except Exception as exc:
                diagnosis = diagnose_error(getattr(exc, 'status', None), getattr(exc, 'body', ''), str(exc)); log.error('ROOT CAUSE url=%s diagnosis=%s', url, diagnosis); manifest['urls'][url] = {'status':'failed','error':str(exc),'diagnosis':diagnosis}
        browser.close()
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat(); manifest['summary'] = {'total':len(urls), 'generated':sum(x['status']=='generated' for x in manifest['urls'].values()), 'failed':sum(x['status']=='failed' for x in manifest['urls'].values())}; (settings.artifacts_dir/'run.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8'); summary = manifest['summary']; log.info('RUN SUMMARY total=%s generated=%s failed=%s', summary['total'], summary['generated'], summary['failed']); return 1 if summary['failed'] else 0

if __name__ == '__main__': raise SystemExit(main())
