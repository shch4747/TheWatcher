import "dotenv/config";

function required(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env var: ${name}`);
  return v;
}

function optional(name: string, fallback = ""): string {
  return process.env[name] ?? fallback;
}

// When true, every "page" the agent would read/write in Lapis is instead a
// plain markdown/JSON file under LOCAL_OUTPUT_DIR on this machine, and no
// Lapis connection is needed at all. Meant for trying a job out (especially
// job 3, which doesn't touch projects/WhatsApp pages) without any Lapis
// setup — see README > Testing without Lapis.
const localMode = optional("LOCAL_MODE", "false").toLowerCase() === "true";

export const config = {
  localMode,
  localOutputDir: optional("LOCAL_OUTPUT_DIR", "./local-output"),

  lapis: {
    baseUrl: (localMode ? optional("LAPIS_BASE_URL") : required("LAPIS_BASE_URL")).replace(/\/$/, ""),
    vaultId: localMode ? optional("LAPIS_VAULT_ID") : required("LAPIS_VAULT_ID"),
    bearerToken: optional("LAPIS_BEARER_TOKEN"),
    sessionCookie: optional("LAPIS_SESSION_COOKIE"),

    // Where project notes live (job 2 reads all of these; jobs 1 & 2 both
    // append matched papers into the individual project's own page). In
    // local mode, this is a subfolder under localOutputDir instead.
    projectsPrefix: optional("LAPIS_PROJECTS_PREFIX", "Projects/"),

    // Page the WhatsApp agent writes its technical-discussion digest to.
    // Job 1 watches this page for changes.
    whatsappPagePath: optional("LAPIS_WHATSAPP_PAGE_PATH", "WhatsApp/ResearchDigest.md"),

    // Shared bucket for relevant finds that don't match any specific
    // project (job 1 writes here when nothing project-specific fits).
    commonResearchPagePath: optional("LAPIS_COMMON_RESEARCH_PAGE_PATH", "Interesting To Research.md"),

    // Standalone "what's trending right now" feed (job 3). Overwritten each
    // run with a fresh snapshot rather than appended to indefinitely.
    trendingPagePath: optional("LAPIS_TRENDING_PAGE_PATH", "Trending Feed.md"),

    // Short human-readable run log so the society can see the agent is
    // alive and what it did, without digging through GitHub Actions logs.
    agentLogPath: optional("LAPIS_AGENT_LOG_PATH", "Research Agent Log.md"),

    // Internal bookkeeping (hashes, dedup set, timestamps) — not meant for
    // members to read, hence the leading dot.
    statePath: optional("LAPIS_STATE_PATH", "Research/.agent-state.json"),
  },

  openrouter: {
    apiKey: required("OPENROUTER_API_KEY"),
    // Defaults to a free (zero-cost) model so a fresh OpenRouter account
    // with $0 credit works out of the box. Free models are rate-limited
    // (roughly 20 req/min, 50 req/day with no credits ever added, 1000/day
    // once you've added $10+ at some point) but that's plenty for this
    // agent's actual call volume — see README > Running for free.
    model: optional("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
    referer: optional("OPENROUTER_SITE_URL", "https://github.com/0xDevansh/lapis"),
    appTitle: optional("OPENROUTER_APP_TITLE", "Lapis Research Agent"),
    // Hard cap applied to every request's max_tokens, regardless of what an
    // individual call asks for. Left generous by default (4000, matching
    // the largest individual call) so it never silently truncates a JSON
    // response — lower it if you're specifically hitting 402/credit errors
    // on a paid model and need to shrink requests further.
    maxTokensCap: Number(optional("OPENROUTER_MAX_TOKENS_CAP", "4000")),
  },

  hfToken: optional("HF_TOKEN"),
  forceRun: optional("FORCE_RUN", "false").toLowerCase() === "true",
  // When true: still reads real data and runs the model, but never actually
  // writes (to Lapis, or to disk in local mode) — logs what it *would* have
  // written instead. Combine with LOCAL_MODE=false to test safely against
  // your real vault, or leave off in LOCAL_MODE to actually see the files.
  dryRun: optional("DRY_RUN", "false").toLowerCase() === "true",

  hfTrendingLimit: Number(optional("HF_TRENDING_LIMIT", "30")),
  hnStoriesLimit: Number(optional("HN_STORIES_LIMIT", "80")),
  // arXiv covers cross-domain topics (e.g. neuroscience+ML) reasonably well
  // by full-text search, so job 1's lookback can be generous — WhatsApp
  // discussions can reference things from a while back.
  arxivLookbackDays: Number(optional("ARXIV_LOOKBACK_DAYS", "21")),

  // Job 2 & 3 cadence floor. Job 1 has no day-based floor — it's driven
  // purely by whether the WhatsApp page changed.
  minDaysBetweenPeriodicRuns: Number(optional("MIN_DAYS_BETWEEN_PERIODIC_RUNS", "2")),

  // How many recently-pushed URLs / recent WhatsApp topics to remember for
  // cross-job dedup (job 3 excludes anything in these; all jobs skip
  // candidates already in this list before even asking the model). Kept
  // large by default since "never push the same paper twice" should hold
  // for the life of the vault, not just a rolling window — a JSON array of
  // a few thousand URLs is still tiny (~150KB) as a state file.
  recentUrlsMemorySize: Number(optional("RECENT_URLS_MEMORY_SIZE", "5000")),
  recentTopicsMemorySize: Number(optional("RECENT_TOPICS_MEMORY_SIZE", "100")),

  // Job 3: cap on how many trending items to keep in the feed page.
  trendingFeedSize: Number(optional("TRENDING_FEED_SIZE", "20")),
};

if (!localMode && !config.lapis.bearerToken && !config.lapis.sessionCookie) {
  throw new Error(
    "Set either LAPIS_BEARER_TOKEN or LAPIS_SESSION_COOKIE so the agent can authenticate to Lapis (or set LOCAL_MODE=true to skip Lapis entirely)."
  );
}
