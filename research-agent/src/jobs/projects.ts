import { loadState, saveState, markPushed } from "../state.js";
import { loadProjectContexts } from "../projectContext.js";
import { fetchTrendingPapers } from "../sources/huggingface.js";
import { searchArxivMulti } from "../sources/arxiv.js";
import { judgeRelevanceForProject } from "../relevance.js";
import { fromHfPaper, fromArxivPaper, dedupeByUrl, type CandidateItem } from "../candidates.js";
import { pushToProjectPage, logPushes, type PushRecord } from "../pages.js";

export async function runProjectsJob(): Promise<void> {
  console.log("[job:projects] Loading project pages...");
  const projects = await loadProjectContexts();
  if (projects.length === 0) {
    console.log("[job:projects] No project pages found — nothing to match against.");
    return;
  }
  console.log(`[job:projects] ${projects.length} projects loaded.`);

  let state = await loadState();
  const trending = await fetchTrendingPapers();
  const trendingCandidates: CandidateItem[] = trending.map(fromHfPaper);

  const allPushes: PushRecord[] = [];

  for (const project of projects) {
    console.log(`[job:projects] Searching arXiv for: ${project.title}`);
    const arxivResults = await searchArxivMulti(project.topics, 5);
    const candidates = dedupeByUrl([...trendingCandidates, ...arxivResults.map((p) => fromArxivPaper(p))]).filter(
      (c) => !state.recentUrls.includes(c.url) // never re-surface an already-pushed paper
    );

    const matches = await judgeRelevanceForProject(project, candidates);
    if (matches.length === 0) continue;

    const pushed = await pushToProjectPage(
      project.path,
      matches.map((m) => ({ candidate: m.candidate, reason: m.reason }))
    );
    allPushes.push(...pushed);
    console.log(`[job:projects]   +${pushed.length} -> ${project.path}`);

    // Mark exactly what was actually written, and do it before the next
    // project's search so a paper relevant to two projects isn't pushed
    // twice within the same run.
    state = markPushed(state, pushed.map((p) => p.candidate.url));
  }

  await saveState({ ...state, lastProjectsRunAt: new Date().toISOString() });
  await logPushes("projects", allPushes);
}
