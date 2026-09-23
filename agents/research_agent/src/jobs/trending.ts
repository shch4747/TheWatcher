import { loadState, saveState, markPushed } from "../state.js";
import { fetchTrendingPapers } from "../sources/huggingface.js";
import { fetchTopHnStories, isArxivLink } from "../sources/hackernews.js";
import { searchArxivMulti } from "../sources/arxiv.js";
import { extractHnTopics, filterTrending } from "../relevance.js";
import { fromHfPaper, fromArxivPaper, fromHnStory, dedupeByUrl, type CandidateItem } from "../candidates.js";
import { writeTrendingFeed, logPushes } from "../pages.js";

export async function runTrendingJob(): Promise<void> {
  console.log("[job:trending] Fetching HF trending papers + HN stories...");
  const [trending, hnStories] = await Promise.all([fetchTrendingPapers(), fetchTopHnStories()]);

  // HN stories that link straight to an arXiv paper are better represented
  // as papers (with an abstract) than as bare articles.
  const hnArxivLinks = hnStories.filter(isArxivLink);
  const hnArticles = hnStories.filter((s) => !isArxivLink(s));

  const hnTopics = await extractHnTopics(hnStories);
  console.log(`[job:trending] Derived HN topics: ${hnTopics.join(", ") || "(none)"}`);
  const hnDerivedPapers = hnTopics.length > 0 ? await searchArxivMulti(hnTopics, 4) : [];

  const state = await loadState();

  const candidates: CandidateItem[] = dedupeByUrl([
    ...trending.map(fromHfPaper),
    ...hnDerivedPapers.map((p) => fromArxivPaper(p)),
    ...hnArticles.map(fromHnStory),
  ]).filter((c) => !state.recentUrls.includes(c.url)); // never re-surface an already-pushed paper

  console.log(`[job:trending] ${candidates.length} candidates after excluding already-pushed items.`);
  console.log(
    `[job:trending] Excluding topics already covered by recent WhatsApp discussion: ${
      state.recentWhatsappTopics.slice(0, 10).join(", ") || "(none)"
    }`
  );

  const matches = await filterTrending(candidates, state.recentWhatsappTopics);
  console.log(`[job:trending] ${matches.length} items judged trending-worthy (before feed-size cap).`);

  // writeTrendingFeed applies the TRENDING_FEED_SIZE cap and returns exactly
  // what ended up on the page — that's what actually counts as "pushed".
  const pushed = await writeTrendingFeed(matches.map((m) => ({ candidate: m.candidate, reason: m.reason })));
  console.log(`[job:trending] ${pushed.length} items written to the feed.`);

  const newState = markPushed(state, pushed.map((p) => p.candidate.url));
  await saveState({ ...newState, lastTrendingRunAt: new Date().toISOString() });

  await logPushes("trending", pushed);
}
