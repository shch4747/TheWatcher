import { config } from "./config.js";

interface OpenRouterChoice {
  message: { role: string; content: string };
}
interface OpenRouterResponse {
  choices: OpenRouterChoice[];
  error?: { message: string };
}

interface OpenRouterModelInfo {
  id: string;
  context_length?: number;
  pricing?: { prompt?: string; completion?: string };
}

// OpenRouter's free-tier roster rotates — a model that's free today can be
// pulled or moved to paid-only without notice (this is exactly what
// happened to the previous default). Rather than hardcoding one slug and
// having it break again later, we try the configured model first and, if
// OpenRouter reports it's no longer free, fetch the *live* list of free
// models and retry with whichever's actually available right now.
const PREFERRED_FREE_MODELS = [
  "meta-llama/llama-3.3-70b-instruct:free",
  "qwen/qwen3-235b-a22b:free",
  "qwen/qwen3-next-80b-a3b-instruct:free",
  "openai/gpt-oss-20b:free",
  "openai/gpt-oss-120b:free",
  "google/gemma-3-27b-it:free",
  "nvidia/nemotron-3-super-120b-a12b:free",
];

let cachedFallbackModel: string | null = null;

async function discoverLiveFreeModel(avoid: string): Promise<string> {
  if (cachedFallbackModel && cachedFallbackModel !== avoid) return cachedFallbackModel;

  const res = await fetch("https://openrouter.ai/api/v1/models");
  if (!res.ok) {
    throw new Error(`Could not list OpenRouter models to find a free fallback: ${res.status}`);
  }
  const data = (await res.json()) as { data: OpenRouterModelInfo[] };
  const free = data.data.filter(
    (m) =>
      m.id !== avoid && // don't hand back the one that just failed
      m.id.endsWith(":free") &&
      m.pricing?.prompt === "0" &&
      m.pricing?.completion === "0"
  );
  if (free.length === 0) {
    throw new Error(
      "No other free models are currently available on OpenRouter. Set OPENROUTER_MODEL to a paid model and add credits, or try again later."
    );
  }

  const preferred = PREFERRED_FREE_MODELS.find((slug) => slug !== avoid && free.some((m) => m.id === slug));
  const chosen = preferred ?? [...free].sort((a, b) => (b.context_length ?? 0) - (a.context_length ?? 0))[0].id;

  cachedFallbackModel = chosen;
  return chosen;
}

async function postChatCompletion(model: string, prompt: string, maxTokens: number): Promise<Response> {
  return fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${config.openrouter.apiKey}`,
      "Content-Type": "application/json",
      // Optional but recommended by OpenRouter for attribution/rankings.
      "HTTP-Referer": config.openrouter.referer,
      "X-Title": config.openrouter.appTitle,
    },
    body: JSON.stringify({
      model,
      max_tokens: maxTokens,
      messages: [{ role: "user", content: prompt }],
    }),
  });
}

/**
 * Sends a single-turn prompt to OpenRouter's OpenAI-compatible chat
 * completions endpoint and returns the model's text response.
 * Model is set via OPENROUTER_MODEL (any "provider/model" slug from
 * https://openrouter.ai/models). If that model is a `:free` slug and
 * OpenRouter reports it's no longer available for free (404), this
 * automatically discovers and retries with a currently-live free model once.
 */
export async function chatComplete(prompt: string, maxTokens = 2000): Promise<string> {
  const cappedMaxTokens = Math.min(maxTokens, config.openrouter.maxTokensCap);
  const primaryModel = config.openrouter.model;

  let res = await postChatCompletion(primaryModel, prompt, cappedMaxTokens);
  let modelUsed = primaryModel;

  if (res.status === 404 && primaryModel.endsWith(":free")) {
    const body = await res.text().catch(() => "");
    console.warn(
      `[openrouter] "${primaryModel}" isn't available for free right now (${body.slice(
        0,
        200
      )}). Looking up a currently-live free model...`
    );
    const fallback = await discoverLiveFreeModel(primaryModel);
    console.warn(
      `[openrouter] Retrying with "${fallback}". To skip this lookup on future runs, set OPENROUTER_MODEL=${fallback} in your .env (worth rechecking occasionally — free-tier availability rotates).`
    );
    res = await postChatCompletion(fallback, prompt, cappedMaxTokens);
    modelUsed = fallback;
  }

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`OpenRouter request failed (model: ${modelUsed}): ${res.status} ${body}`);
  }

  const data = (await res.json()) as OpenRouterResponse;
  if (data.error) throw new Error(`OpenRouter error (model: ${modelUsed}): ${data.error.message}`);
  const content = data.choices?.[0]?.message?.content;
  if (!content) throw new Error(`OpenRouter response had no content (model: ${modelUsed}): ${JSON.stringify(data)}`);
  return content;
}

export function stripFences(text: string): string {
  return text
    .trim()
    .replace(/^```(json)?/i, "")
    .replace(/```$/, "")
    .trim();
}
