import { config } from "../config.js";

export interface HnStory {
  id: number;
  title: string;
  url?: string; // external link, if any (self-posts / Ask HN have none)
  score: number;
  hnUrl: string;
}

const HN_BASE = "https://hacker-news.firebaseio.com/v0";

// Cheap pre-filter so we don't burn model tokens classifying every story on
// the front page (politics, startups-hiring, etc.) before topic extraction.
const AI_ML_HINTS =
  /\b(ai|artificial intelligence|machine learning|ml|llm|neural|deep learning|transformer|diffusion|gpt|nlp|computer vision|reinforcement learning|dataset|robotics|agent|inference|gpu|cuda)\b/i;

export async function fetchTopHnStories(limit = config.hnStoriesLimit): Promise<HnStory[]> {
  const idsRes = await fetch(`${HN_BASE}/topstories.json`);
  if (!idsRes.ok) throw new Error(`HN topstories request failed: ${idsRes.status}`);
  const allIds = (await idsRes.json()) as number[];
  const ids = allIds.slice(0, limit);

  interface HnItem {
    id: number;
    type?: string;
    title?: string;
    url?: string;
    score?: number;
  }

  const stories = await Promise.all(
    ids.map(async (id) => {
      const res = await fetch(`${HN_BASE}/item/${id}.json`);
      if (!res.ok) return null;
      const item = (await res.json()) as HnItem | null;
      if (!item || item.type !== "story" || !item.title) return null;
      return {
        id: item.id,
        title: item.title,
        url: item.url,
        score: item.score ?? 0,
        hnUrl: `https://news.ycombinator.com/item?id=${item.id}`,
      } as HnStory;
    })
  );

  return stories
    .filter((s): s is HnStory => s !== null)
    .filter((s) => AI_ML_HINTS.test(s.title))
    .sort((a, b) => b.score - a.score);
}

/** True if the story's outbound link points at an arXiv abstract/PDF (so it's
 * better represented as a paper via the arXiv source than as a generic article). */
export function isArxivLink(story: HnStory): boolean {
  return !!story.url && /arxiv\.org/i.test(story.url);
}
