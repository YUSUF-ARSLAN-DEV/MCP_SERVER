import 'dotenv/config';
import { generate as openaiCompatible } from './providers/openai-compatible.mjs';

/**
 * Provider registry. Pick one with LLM_PROVIDER in .env (default: openai-compatible).
 * Every provider exports `generate(prompt, { system?, log? }) -> Promise<string>`,
 * so the rest of the codebase never knows or cares which one is active.
 *
 * To add e.g. Anthropic later:
 *   1. create providers/anthropic.mjs exporting the same `generate` signature
 *   2. add it to PROVIDERS below
 *   3. set LLM_PROVIDER=anthropic in .env
 */
const PROVIDERS = {
  'openai-compatible': openaiCompatible,
  openai: openaiCompatible, // alias
};

const NAME = (process.env.LLM_PROVIDER || 'openai-compatible').toLowerCase();
const provider = PROVIDERS[NAME];
if (!provider) {
  throw new Error(`Unknown LLM_PROVIDER "${NAME}". Available: ${Object.keys(PROVIDERS).join(', ')}`);
}

export function generate(prompt, opts) {
  return provider(prompt, opts);
}

export const providerName = NAME;
