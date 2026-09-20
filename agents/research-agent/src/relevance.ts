import { chatComplete, stripFences } from "./openrouter.js";
import type { CandidateItem } from "./candidates.js";
import type { HnStory } from "./sources/hackernews.js";

export interface ProjectContext {
  path: string;
  title: string;
  status: "ongoing" | "completed" | "unknown";
  topics: string[];
  oneLiner: string;
}

/** Extracts a short topic list + one-liner per project note, in one call. */
export async function extractProjectContexts(
  projects: { path: string; content: string }[]
): Promise<ProjectContext[]> {
  if (projects.length === 0) return [];

  const prompt = `You are helping an AI/ML university society's research-recommendation agent.
Below are project notes from the society's vault. For EACH project, extract:
- "title": a short human-readable project name
- "status": "ongoing", "completed", or "unknown" (infer from content/frontmatter if not explicit)
- "topics": 3-6 short keyword phrases suitable as search queries on arXiv (e.g. "retrieval augmented generation", "graph neural networks for molecules")
- "oneLiner": one sentence describing what the project does

Respond with ONLY a JSON array, one object per project, in the same order as given, each with fields: path, title, status, topics, oneLiner. No prose, no markdown fences.

Projects:
${projects
  .map((p, i) => `--- Project ${i + 1} (path: ${p.path}) ---\n${p.content.slice(0, 4000)}`)
  .join("\n\n")}`;

  const text = await chatComplete(prompt, 4000);
  return JSON.parse(stripFences(text)) as ProjectContext[];
}

export interface WhatsappExtraction {
  topics: string[]; // arXiv-search-friendly phrases, always framed through an AI/ML lens
  summary: string; // one-paragraph gist, for the run log
}

/**
 * Extracts AI/ML-relevant research topics from the WhatsApp technical
 * digest. Deliberately does NOT search on whatever raw subject came up —
 * every topic is reframed through an AI/ML angle before it's used to query
 * arXiv, so a discussion of e.g. neuroscience becomes "neuroscience-inspired
 * neural network architectures" rather than plain "neuroscience", and a
 * discussion with no meaningful AI/ML connection is dropped entirely.
 */
export async function extractWhatsappTopics(content: string): Promise<WhatsappExtraction> {
  const prompt = `Below is a digest of technical discussion from an AI/ML university society's WhatsApp community (already summarized by another agent).

Your job is to turn this into AI/ML-relevant arXiv search topics — NOT a general research-topic list.

For each substantive technical thing discussed:
- If it's already AI/ML (a model, technique, paper, tool, etc.), extract it directly as a topic.
- If it's from an adjacent field (e.g. neuroscience, biology, hardware, physics, social science), reframe it as the AI/ML angle or intersection specifically — e.g. a discussion of "neuroscience" becomes "neuroscience-inspired neural network architectures" or "computational models of the brain", not "neuroscience" on its own. Only do this if there's a genuine, non-forced AI/ML connection to draw.
- If a discussed topic has no meaningful AI/ML connection at all, drop it — don't invent one.
- Always skip purely social/logistics content (meeting times, event planning).

Return:
- "topics": 3-8 short, specific, arXiv-search-friendly phrases, each already framed through the AI/ML lens per the rules above.
- "summary": one short paragraph gisting what was discussed, for a human reading a run log.

Respond with ONLY a JSON object with fields "topics" (array of strings) and "summary" (string). No prose, no markdown fences.

Digest:
${content.slice(0, 8000)}`;

  const text = await chatComplete(prompt, 1500);
  return JSON.parse(stripFences(text)) as WhatsappExtraction;
}

/** Extracts AI/ML research topics worth searching arXiv for, from HN story titles. */
export async function extractHnTopics(stories: HnStory[]): Promise<string[]> {
  if (stories.length === 0) return [];

  const prompt = `Below are Hacker News story titles already pre-filtered for AI/ML relevance.
Pick out the ones that point to a genuine research topic or technique (skip pure product launches, funding news, or opinion pieces with no research angle), and return 3-10 short arXiv-search-friendly topic phrases summarizing what's currently getting attention.

Respond with ONLY a JSON array of strings. No prose, no markdown fences.

Titles:
${stories.map((s) => `- ${s.title}`).join("\n")}`;

  const text = await chatComplete(prompt, 1000);
  return JSON.parse(stripFences(text)) as string[];
}

export interface Assignment {
  candidate: CandidateItem;
  reason: string;
  projectPath: string | null; // null => goes to the shared "common" page
}

/**
 * Used by job 1 (WhatsApp-triggered). For each candidate, decides whether
 * it's genuinely worth surfacing, and if so whether it belongs on a
 * specific project's page or the shared "Interesting / To Research" page.
 * Batches everything into one call per invocation.
 */
export async function assignCandidates(
  candidates: CandidateItem[],
  projects: ProjectContext[],
  whatsappSummary: string
): Promise<Assignment[]> {
  if (candidates.length === 0) return [];

  const projectList =
    projects.length > 0
      ? projects.map((p, i) => `${i + 1}. "${p.title}" (path: ${p.path}) — ${p.oneLiner}`).join("\n")
      : "(no active projects on file)";

  const prompt = `An AI/ML university society's WhatsApp community recently discussed:
${whatsappSummary}

The society's current/past projects are:
${projectList}

Below are candidate papers/articles found in response to that discussion. For EACH candidate that is genuinely worth a member reading, decide:
- "id": the candidate's id, exactly as given
- "reason": one short sentence on why it's relevant / worth reading
- "projectPath": the "path" of the single best-matching project from the list above if it clearly relates to that project's specific work, otherwise null (meaning: relevant to the discussion generally, but not tied to one project)

Only include candidates that are genuinely AI/ML-relevant (or a clear, non-forced AI/ML intersection with another field, e.g. neuroscience-inspired ML) — skip anything only tangentially related or from an unrelated field with no real AI/ML connection.

Respond with ONLY a JSON array of such objects, omitting candidates that aren't worth surfacing at all. No prose, no markdown fences.

Candidates:
${candidates
  .map(
    (c, i) =>
      `${i + 1}. id="${c.id}" kind=${c.kind} title="${c.title}"${
        c.summary ? `\n   summary: ${c.summary.slice(0, 400)}` : ""
      }`
  )
  .join("\n")}`;

  const text = await chatComplete(prompt, 3000);
  const picks = JSON.parse(stripFences(text)) as {
    id: string;
    reason: string;
    projectPath: string | null;
  }[];

  const byId = new Map(candidates.map((c) => [c.id, c]));
  return picks
    .filter((p) => byId.has(p.id))
    .map((p) => ({ candidate: byId.get(p.id)!, reason: p.reason, projectPath: p.projectPath }));
}

export interface RelevanceMatch {
  candidate: CandidateItem;
  reason: string;
}

/**
 * Used by job 2 (periodic, project-relevant). For one project, judges which
 * candidates are genuinely relevant to it specifically.
 */
export async function judgeRelevanceForProject(
  project: ProjectContext,
  candidates: CandidateItem[]
): Promise<RelevanceMatch[]> {
  if (candidates.length === 0) return [];

  const prompt = `Project: "${project.title}" — ${project.oneLiner}
Project topics: ${project.topics.join(", ")}

Below are candidate research papers/articles. Select ONLY the ones that are genuinely relevant to this specific project (not just tangentially AI-related). For each selected item, write one short sentence on why it's relevant to this project.

Respond with ONLY a JSON array of objects with fields "id" (matching the candidate's id) and "reason". Omit items that aren't relevant. No prose, no markdown fences.

Candidates:
${candidates
  .map(
    (c, i) =>
      `${i + 1}. id="${c.id}" kind=${c.kind} title="${c.title}"${
        c.summary ? `\n   summary: ${c.summary.slice(0, 400)}` : ""
      }`
  )
  .join("\n")}`;

  const text = await chatComplete(prompt, 2000);
  const picks = JSON.parse(stripFences(text)) as { id: string; reason: string }[];

  const byId = new Map(candidates.map((c) => [c.id, c]));
  return picks
    .filter((p) => byId.has(p.id))
    .map((p) => ({ candidate: byId.get(p.id)!, reason: p.reason }));
}

/**
 * Used by job 3 (periodic, general trending). Filters candidates down to
 * ones worth surfacing as "trending" — excluding topics already covered by
 * recent WhatsApp discussion (those get handled by job 1 instead) — and
 * writes a one-line blurb for each. Sorting by popularity happens outside
 * this call, using each candidate's `popularity` field.
 */
export async function filterTrending(
  candidates: CandidateItem[],
  recentlyDiscussedTopics: string[]
): Promise<RelevanceMatch[]> {
  if (candidates.length === 0) return [];

  const prompt = `Below are trending AI/ML papers and tech articles/news, for a university AI/ML society's "what's trending" feed.

Topics the society has ALREADY discussed recently (exclude candidates that are essentially about these — they're covered elsewhere, this feed is for genuinely new-to-them content):
${recentlyDiscussedTopics.length > 0 ? recentlyDiscussedTopics.join(", ") : "(none on file)"}

For each candidate genuinely worth including, write one short, punchy sentence on why it's interesting/relevant to an AI/ML student society (not a generic summary — say why it matters).

Respond with ONLY a JSON array of objects with fields "id" and "reason". Omit candidates that overlap with the already-discussed topics above, or that are low-quality/irrelevant. No prose, no markdown fences.

Candidates:
${candidates
  .map(
    (c, i) =>
      `${i + 1}. id="${c.id}" kind=${c.kind} source=${c.source} title="${c.title}"${
        c.summary ? `\n   summary: ${c.summary.slice(0, 400)}` : ""
      }`
  )
  .join("\n")}`;

  const text = await chatComplete(prompt, 3000);
  const picks = JSON.parse(stripFences(text)) as { id: string; reason: string }[];

  const byId = new Map(candidates.map((c) => [c.id, c]));
  return picks
    .filter((p) => byId.has(p.id))
    .map((p) => ({ candidate: byId.get(p.id)!, reason: p.reason }));
}
