import { config } from "../config.js";

export interface HfPaper {
  id: string; // arXiv id
  title: string;
  summary: string;
  upvotes?: number;
  publishedAt?: string;
  url: string;
}

interface HfDailyPaperResponse {
  paper: {
    id: string;
    title: string;
    summary: string;
    publishedAt?: string;
  };
  numUpvotes?: number;
  publishedAt?: string;
}

/** Pulls today's trending papers from Hugging Face's Daily Papers feed. */
export async function fetchTrendingPapers(limit = config.hfTrendingLimit): Promise<HfPaper[]> {
  const url = new URL("https://huggingface.co/api/daily_papers");
  url.searchParams.set("limit", String(Math.min(limit, 100)));
  url.searchParams.set("sort", "trending");

  const headers: Record<string, string> = {};
  if (config.hfToken) headers.Authorization = `Bearer ${config.hfToken}`;

  const res = await fetch(url, { headers });
  if (!res.ok) {
    throw new Error(`Hugging Face daily_papers request failed: ${res.status}`);
  }
  const data = (await res.json()) as HfDailyPaperResponse[];

  return data.slice(0, limit).map((entry) => ({
    id: entry.paper.id,
    title: entry.paper.title,
    summary: entry.paper.summary,
    upvotes: entry.numUpvotes,
    publishedAt: entry.publishedAt ?? entry.paper.publishedAt,
    url: `https://huggingface.co/papers/${entry.paper.id}`,
  }));
}
