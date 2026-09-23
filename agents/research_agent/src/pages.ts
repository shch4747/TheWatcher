import type { CandidateItem } from "./candidates.js";
import { appendUnderHeading, overwritePage, appendLogLines } from "./lapisClient.js";
import { config } from "./config.js";

const SUGGESTED_RESEARCH_HEADING = "🔎 Suggested Research (auto)";
const today = () => new Date().toISOString().slice(0, 10);

export interface PushItem {
  candidate: CandidateItem;
  reason: string;
}

/** A single push that actually happened — used to build the datalog. */
export interface PushRecord {
  candidate: CandidateItem;
  destination: string; // vault path the item was written to
}

function renderBullet(
  candidate: CandidateItem,
  reason: string
): { line: string; dedupeKey: string; candidate: CandidateItem } {
  const kindLabel = candidate.kind === "paper" ? candidate.source : "article";
  const line = `- **[${candidate.title}](${candidate.url})** _(${kindLabel}, added ${today()})_ — ${reason}`;
  return { line, dedupeKey: candidate.url, candidate };
}

/** Appends matched candidates to a specific project's page. Returns only the
 * items that were actually newly written (already-present ones are skipped
 * by `appendUnderHeading`'s dedupe check against that page's own content). */
export async function pushToProjectPage(
  projectPath: string,
  items: PushItem[]
): Promise<PushRecord[]> {
  const lines = items.map((i) => renderBullet(i.candidate, i.reason));
  const { added } = await appendUnderHeading(projectPath, SUGGESTED_RESEARCH_HEADING, lines);
  return added.map((a) => ({ candidate: a.candidate, destination: projectPath }));
}

/** Appends matched-but-unassigned candidates to the shared common page. */
export async function pushToCommonPage(items: PushItem[]): Promise<PushRecord[]> {
  const lines = items.map((i) => renderBullet(i.candidate, i.reason));
  const { added } = await appendUnderHeading(
    config.lapis.commonResearchPagePath,
    "Interesting / To Research",
    lines,
    "Things relevant to what's being discussed, that don't map to one specific project."
  );
  return added.map((a) => ({ candidate: a.candidate, destination: config.lapis.commonResearchPagePath }));
}

/** Overwrites the trending feed page with a fresh, popularity-sorted
 * snapshot. Every item passed in here is, by construction (callers filter
 * against already-pushed URLs before calling this), new — so all of them
 * count as pushes for the datalog. */
export async function writeTrendingFeed(items: PushItem[]): Promise<PushRecord[]> {
  const sorted = [...items].sort(
    (a, b) => (b.candidate.popularity ?? 0) - (a.candidate.popularity ?? 0)
  );
  const capped = sorted.slice(0, config.trendingFeedSize);

  const lines = [
    "---",
    "tags: [trending-feed, auto-generated]",
    `updated: ${today()}`,
    "---",
    "",
    `# Trending Feed`,
    "",
    `> Auto-generated snapshot, updated ${today()}. Popular AI/ML papers and tech news not yet covered elsewhere.`,
    "",
  ];

  if (capped.length === 0) {
    lines.push("Nothing new this run.");
  } else {
    for (const { candidate, reason } of capped) {
      const kindLabel = candidate.kind === "paper" ? candidate.source : "article";
      const pop = candidate.popularity != null ? ` · ${candidate.popularity} pts` : "";
      lines.push(`- **[${candidate.title}](${candidate.url})** _(${kindLabel}${pop})_ — ${reason}`);
    }
  }

  await overwritePage(config.lapis.trendingPagePath, lines.join("\n") + "\n");
  return capped.map((c) => ({ candidate: c.candidate, destination: config.lapis.trendingPagePath }));
}

/**
 * Writes the datalog: one dated, timestamped line per item actually pushed
 * in this job run, plus a one-line summary. This is the audit trail members
 * can check to see exactly what the agent did and when — every push is
 * individually registered here, not just summarized.
 */
export async function logPushes(job: string, pushes: PushRecord[]): Promise<void> {
  const now = new Date();
  const ts = now.toISOString().replace("T", " ").slice(0, 19) + " UTC";

  const lines: string[] = [];
  for (const p of pushes) {
    lines.push(
      `- \`${ts}\` **[${job}]** pushed [${p.candidate.title}](${p.candidate.url}) → \`${p.destination}\``
    );
  }
  lines.push(
    `- \`${ts}\` **[${job}]** run complete — ${pushes.length} item(s) pushed.`
  );

  await appendLogLines(config.lapis.agentLogPath, lines);
}
