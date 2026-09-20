import type { HfPaper } from "./sources/huggingface.js";
import type { ArxivPaper } from "./sources/arxiv.js";
import type { HnStory } from "./sources/hackernews.js";

export type CandidateSource = "huggingface" | "arxiv" | "hackernews";
export type CandidateKind = "paper" | "article";

export interface CandidateItem {
  kind: CandidateKind;
  source: CandidateSource;
  id: string; // stable-ish id for LLM reference (not necessarily the URL)
  title: string;
  summary: string; // abstract for papers, empty/short for articles (title-only)
  url: string;
  popularity?: number; // upvotes / HN score, for job 3 sorting
}

export function fromHfPaper(p: HfPaper): CandidateItem {
  return {
    kind: "paper",
    source: "huggingface",
    id: `hf:${p.id}`,
    title: p.title,
    summary: p.summary,
    url: p.url,
    popularity: p.upvotes,
  };
}

export function fromArxivPaper(p: ArxivPaper): CandidateItem {
  return {
    kind: "paper",
    source: "arxiv",
    id: `arxiv:${p.id}`,
    title: p.title,
    summary: p.summary,
    url: p.url,
  };
}

/** HN stories are represented as generic "articles" (title + link only —
 * we don't fetch and summarize the linked page, to keep this cheap/robust). */
export function fromHnStory(s: HnStory): CandidateItem {
  return {
    kind: "article",
    source: "hackernews",
    id: `hn:${s.id}`,
    title: s.title,
    summary: "",
    url: s.url ?? s.hnUrl,
    popularity: s.score,
  };
}

export function dedupeById(items: CandidateItem[]): CandidateItem[] {
  const seen = new Map<string, CandidateItem>();
  for (const c of items) if (!seen.has(c.id)) seen.set(c.id, c);
  return [...seen.values()];
}

export function dedupeByUrl(items: CandidateItem[]): CandidateItem[] {
  const seen = new Map<string, CandidateItem>();
  for (const c of items) if (!seen.has(c.url)) seen.set(c.url, c);
  return [...seen.values()];
}
