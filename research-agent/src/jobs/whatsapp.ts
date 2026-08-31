import { config } from "../config.js";
import { readFile } from "../lapisClient.js";
import { loadState, saveState, whatsappChanged, markPushed, addWhatsappTopics, hash } from "../state.js";
import { loadProjectContexts } from "../projectContext.js";
import { fetchTrendingPapers } from "../sources/huggingface.js";
import { searchArxivMulti } from "../sources/arxiv.js";
import { extractWhatsappTopics, assignCandidates } from "../relevance.js";
import { fromHfPaper, fromArxivPaper, dedupeByUrl, type CandidateItem } from "../candidates.js";
import { pushToProjectPage, pushToCommonPage, logPushes, type PushRecord } from "../pages.js";

export async function runWhatsappJob(): Promise<void> {
  console.log("[job:whatsapp] Reading WhatsApp digest page...");
  const content = await readFile(config.lapis.whatsappPagePath);
  if (content === null) {
    console.log(`[job:whatsapp] No page found at "${config.lapis.whatsappPagePath}" — nothing to do.`);
    return;
  }

  const state = await loadState();
  if (!whatsappChanged(state, content)) {
    console.log("[job:whatsapp] Page unchanged since last run — skipping.");
    return;
  }
  console.log("[job:whatsapp] Page changed — running full pass.");

  // Topics here are already reframed through an AI/ML lens (or dropped if
  // there's no genuine AI/ML connection) — see extractWhatsappTopics.
  const { topics, summary } = await extractWhatsappTopics(content);
  console.log(`[job:whatsapp] AI/ML-framed topics: ${topics.join(", ") || "(none)"}`);

  if (topics.length === 0) {
    console.log("[job:whatsapp] Nothing with an AI/ML connection found — updating state and exiting.");
    await saveState({ ...state, lastWhatsappHash: hash(content), lastWhatsappRunAt: new Date().toISOString() });
    await logPushes("whatsapp", []);
    return;
  }

  const [trending, arxivResults] = await Promise.all([
    fetchTrendingPapers(),
    searchArxivMulti(topics, 5),
  ]);

  const candidates: CandidateItem[] = dedupeByUrl([
    ...trending.map(fromHfPaper),
    ...arxivResults.map(fromArxivPaper),
  ]).filter((c) => !state.recentUrls.includes(c.url)); // never re-surface an already-pushed paper

  console.log(`[job:whatsapp] ${candidates.length} candidate papers to evaluate (after excluding already-pushed).`);

  const projects = await loadProjectContexts();
  const assignments = await assignCandidates(candidates, projects, summary);
  console.log(`[job:whatsapp] ${assignments.length} candidates judged relevant.`);

  const toCommon = assignments.filter((a) => !a.projectPath);
  const byProject = new Map<string, typeof assignments>();
  for (const a of assignments) {
    if (!a.projectPath) continue;
    if (!byProject.has(a.projectPath)) byProject.set(a.projectPath, []);
    byProject.get(a.projectPath)!.push(a);
  }

  const allPushes: PushRecord[] = [];
  for (const [projectPath, items] of byProject) {
    const pushed = await pushToProjectPage(
      projectPath,
      items.map((i) => ({ candidate: i.candidate, reason: i.reason }))
    );
    allPushes.push(...pushed);
    console.log(`[job:whatsapp]   +${pushed.length} -> ${projectPath}`);
  }

  const commonPushed = await pushToCommonPage(toCommon.map((i) => ({ candidate: i.candidate, reason: i.reason })));
  allPushes.push(...commonPushed);
  console.log(`[job:whatsapp]   +${commonPushed.length} -> ${config.lapis.commonResearchPagePath}`);

  // Mark exactly what was actually written (not just "judged relevant") as
  // pushed, so a paper a project page already had (e.g. added by a human)
  // doesn't wrongly get remembered as agent-pushed.
  let s = markPushed(state, allPushes.map((p) => p.candidate.url));
  s = addWhatsappTopics(s, topics);
  s = { ...s, lastWhatsappHash: hash(content), lastWhatsappRunAt: new Date().toISOString() };
  await saveState(s);

  await logPushes("whatsapp", allPushes);
}
