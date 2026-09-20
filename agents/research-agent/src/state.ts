import { createHash } from "node:crypto";
import { config } from "./config.js";
import { readFile, writeFile } from "./lapisClient.js";

export interface AgentState {
  lastWhatsappHash: string;
  lastWhatsappRunAt: string | null; // ISO
  // Job 2 & 3 track their own "last full run" independently, since both can
  // be invoked in the same process (e.g. `--job=periodic`) and each needs
  // to make its own due/not-due decision without the other's run masking it.
  lastProjectsRunAt: string | null;
  lastTrendingRunAt: string | null;
  // URLs already pushed anywhere (project page, common page, or trending
  // feed), newest first, capped — prevents the same item resurfacing
  // across jobs/runs.
  recentUrls: string[];
  // Topic phrases pulled from recent WhatsApp discussions, newest first,
  // capped — job 3 uses this to avoid re-surfacing what's already been
  // discussed as "new" trending content.
  recentWhatsappTopics: string[];
}

const EMPTY_STATE: AgentState = {
  lastWhatsappHash: "",
  lastWhatsappRunAt: null,
  lastProjectsRunAt: null,
  lastTrendingRunAt: null,
  recentUrls: [],
  recentWhatsappTopics: [],
};

function hash(content: string): string {
  return createHash("sha256").update(content).digest("hex");
}
export { hash };

export async function loadState(): Promise<AgentState> {
  const raw = await readFile(config.lapis.statePath);
  if (!raw) return { ...EMPTY_STATE };
  try {
    return { ...EMPTY_STATE, ...(JSON.parse(raw) as Partial<AgentState>) };
  } catch {
    return { ...EMPTY_STATE };
  }
}

export async function saveState(state: AgentState): Promise<void> {
  await writeFile(config.lapis.statePath, JSON.stringify(state, null, 2));
}

export function isRecentlyPushed(state: AgentState, url: string): boolean {
  return state.recentUrls.includes(url);
}

export function markPushed(state: AgentState, urls: string[]): AgentState {
  const merged = [...urls, ...state.recentUrls];
  const deduped = [...new Set(merged)].slice(0, config.recentUrlsMemorySize);
  return { ...state, recentUrls: deduped };
}

export function addWhatsappTopics(state: AgentState, topics: string[]): AgentState {
  const merged = [...topics, ...state.recentWhatsappTopics];
  const deduped = [...new Set(merged)].slice(0, config.recentTopicsMemorySize);
  return { ...state, recentWhatsappTopics: deduped };
}

/** Job 1 trigger: only true when the WhatsApp page's content actually changed. */
export function whatsappChanged(state: AgentState, currentContent: string): boolean {
  if (config.forceRun) return true;
  return hash(currentContent) !== state.lastWhatsappHash;
}

/** Jobs 2 & 3 trigger: true if that job never ran, or >= minDaysBetweenPeriodicRuns elapsed. */
export function periodicDue(lastRunAt: string | null): boolean {
  if (config.forceRun) return true;
  if (!lastRunAt) return true;
  const daysSince = (Date.now() - new Date(lastRunAt).getTime()) / (1000 * 60 * 60 * 24);
  return daysSince >= config.minDaysBetweenPeriodicRuns;
}
