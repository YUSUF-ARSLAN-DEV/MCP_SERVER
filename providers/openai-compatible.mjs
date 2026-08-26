import 'dotenv/config';

/**
 * OpenAI-compatible chat-completions adapter.
 * Works with ANY provider that speaks POST /v1/chat/completions:
 * local Ollama / LM Studio / vLLM, plus Groq, Together, OpenRouter, OpenAI, etc.
 * Swap providers by editing .env — no code change.
 *
 * Config (env), with backward-compatible aliases:
 *   API_URL   | LLM_BASE_URL   full endpoint (…/chat/completions) OR base (…/v1 or bare host)
 *   MODEL_NAME| LLM_MODEL      model id the server advertises (GET /v1/models)
 *   API_KEY   | LLM_API_KEY    bearer token (omit for keyless local servers)
 */

const API_URL = process.env.API_URL || process.env.LLM_BASE_URL || 'https://llm-1.d4done.com/v1/chat/completions';
const MODEL_NAME = process.env.MODEL_NAME || process.env.LLM_MODEL || 'google/gemma-4-26b-a4b-qat';
const API_KEY = process.env.API_KEY || process.env.LLM_API_KEY || '';
const MODEL_TIMEOUT_MS = Number(process.env.MODEL_TIMEOUT_MS || 300000);
const MODEL_RETRIES = Number(process.env.MODEL_RETRIES || 4);
const RETRY_BASE_MS = Number(process.env.RETRY_BASE_MS || 3000);
const TEMPERATURE = Number(process.env.MODEL_TEMPERATURE ?? 0.1);
const MAX_TOKENS = Number(process.env.MODEL_MAX_TOKENS || 3072);

// Accept a full .../chat/completions URL, a base ".../v1", or a bare host.
function endpoint() {
  let u = API_URL.trim().replace(/\/+$/, '');
  if (/\/chat\/completions$/.test(u)) return u;
  if (!/\/v\d+$/.test(u)) u += '/v1';
  return `${u}/chat/completions`;
}

let requestNumber = 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const preview = (v, n = 700) => String(v ?? '').replace(/\s+/g, ' ').slice(0, n);
const isTransientStatus = (s) => [408, 409, 425, 429, 500, 502, 503, 504, 524].includes(s);

/**
 * @param {string} prompt
 * @param {{ system?: string, log?: (msg: string, details?: string) => void }} [opts]
 * @returns {Promise<string>} raw assistant text
 */
export async function generate(prompt, { system = 'You are a test spec generator. Output ONLY the TypeScript code block.', log = () => {} } = {}) {
  const messages = [
    { role: 'system', content: system },
    { role: 'user', content: prompt },
  ];
  const url = endpoint();

  for (let attempt = 1; attempt <= MODEL_RETRIES; attempt++) {
    const requestId = ++requestNumber;
    const started = Date.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), MODEL_TIMEOUT_MS);
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {}),
        },
        body: JSON.stringify({ model: MODEL_NAME, messages, temperature: TEMPERATURE, max_tokens: MAX_TOKENS, stream: false }),
        signal: controller.signal,
      });
      const body = await resp.text();
      log(`request #${requestId} response`, `attempt=${attempt}/${MODEL_RETRIES} status=${resp.status} ms=${Date.now() - started} bytes=${body.length}`);
      if (!resp.ok) {
        const error = new Error(`API error: ${resp.status}`);
        error.status = resp.status;
        error.body = body.slice(0, 1000);
        if (!isTransientStatus(resp.status) || attempt === MODEL_RETRIES) throw error;
        log('transient server error; retrying', `request=#${requestId} status=${resp.status} waitMs=${RETRY_BASE_MS * 2 ** (attempt - 1)} body=${preview(body)}`);
        await sleep(RETRY_BASE_MS * 2 ** (attempt - 1));
        continue;
      }
      let data;
      try {
        data = JSON.parse(body);
      } catch {
        const error = new Error('API returned non-JSON success response');
        error.transient = true;
        error.body = body.slice(0, 1000);
        throw error;
      }
      let content = data.choices?.[0]?.message?.content ?? data.output_text ?? data.response ?? data.content;
      if (Array.isArray(content)) content = content.map((part) => (typeof part === 'string' ? part : part.text || '')).join('');
      if (!content) {
        const error = new Error('API response had no usable model content');
        error.transient = true;
        error.body = `keys=${Object.keys(data).join(',')} preview=${preview(JSON.stringify(data))}`;
        throw error;
      }
      log(`request #${requestId} accepted`, `contentChars=${String(content).length} keys=${Object.keys(data).join(',')}`);
      return content;
    } catch (error) {
      const transient = error.transient || error.name === 'AbortError' || error.code === 'ECONNRESET' || isTransientStatus(error.status);
      log(`request #${requestId} failed`, `attempt=${attempt}/${MODEL_RETRIES} ms=${Date.now() - started} type=${error.name || 'Error'} message=${error.message} detail=${preview(error.body)}`);
      if (!transient || attempt === MODEL_RETRIES) throw error;
      log('retrying model request', `request=#${requestId} waitMs=${RETRY_BASE_MS * 2 ** (attempt - 1)}`);
      await sleep(RETRY_BASE_MS * 2 ** (attempt - 1));
    } finally {
      clearTimeout(timeout);
    }
  }
}

export const info = { provider: 'openai-compatible', endpoint: endpoint(), model: MODEL_NAME };
