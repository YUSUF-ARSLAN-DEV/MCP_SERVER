import { chromium } from 'playwright';
import { mkdir } from 'fs/promises';
import readline from 'readline';

// Change this to the site's login page if different.
const LOGIN_URL = 'https://sat.aljazeera.net/en';
const STATE_PATH = '.auth/state.json';

const browser = await chromium.launch({ headless: false }); // visible so you can log in
const context = await browser.newContext();
const page = await context.newPage();
await page.goto(LOGIN_URL);

console.log('\n>> Sign in in the opened browser window (SSO is fine).');
console.log('>> When you are fully logged in, return here and press ENTER.\n');

await new Promise((resolve) => {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  rl.question('', () => { rl.close(); resolve(); });
});

await mkdir('.auth', { recursive: true });
await context.storageState({ path: STATE_PATH }); // saves cookies + localStorage
console.log(`Saved login session to ${STATE_PATH}`);

await browser.close();
