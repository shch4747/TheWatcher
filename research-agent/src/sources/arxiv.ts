import { XMLParser } from "fast-xml-parser";
import { config } from "../config.js";

export interface ArxivPaper {
  id: string; // e.g. 2508.12345
  title: string;
  summary: string;
  authors: string[];
  published: string;
  url: string;
}

const parser = new XMLParser({ ignoreAttributes: false, attributeNamePrefix: "@_" });

function idFromEntryId(entryId: string): string {
  // entryId looks like http://arxiv.org/abs/2508.12345v1
  const match = entryId.match(/abs\/([^v]+)/);
  return match ? match[1] : entryId;
}

/**
 * Searches arXiv for recent papers matching a free-text topic/keyword.
 * Restricts to titles/abstracts and to the configured lookback window.
 */
export async function searchArxiv(
  topic: string,
  maxResults = 8
): Promise<ArxivPaper[]> {
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - config.arxivLookbackDays);

  const searchQuery = `all:${JSON.stringify(topic)}`;
  const url = new URL("http://export.arxiv.org/api/query");
  url.searchParams.set("search_query", searchQuery);
  url.searchParams.set("sortBy", "submittedDate");
  url.searchParams.set("sortOrder", "descending");
  url.searchParams.set("max_results", String(maxResults));

  const res = await fetch(url);
  if (!res.ok) throw new Error(`arXiv query failed for "${topic}": ${res.status}`);
  const xml = await res.text();
  const parsed = parser.parse(xml);

  const rawEntries = parsed.feed?.entry;
  if (!rawEntries) return [];
  const entries = Array.isArray(rawEntries) ? rawEntries : [rawEntries];

  const papers: ArxivPaper[] = entries.map((e: any) => {
    const authorsRaw = Array.isArray(e.author) ? e.author : [e.author];
    return {
      id: idFromEntryId(e.id),
      title: String(e.title).replace(/\s+/g, " ").trim(),
      summary: String(e.summary).replace(/\s+/g, " ").trim(),
      authors: authorsRaw.filter(Boolean).map((a: any) => a.name),
      published: e.published,
      url: e.id,
    };
  });

  return papers.filter((p) => new Date(p.published) >= cutoff);
}

/** Runs searchArxiv across several topics and de-duplicates by arXiv id. */
export async function searchArxivMulti(topics: string[], perTopic = 5): Promise<ArxivPaper[]> {
  const seen = new Map<string, ArxivPaper>();
  for (const topic of topics) {
    try {
      const results = await searchArxiv(topic, perTopic);
      for (const p of results) if (!seen.has(p.id)) seen.set(p.id, p);
    } catch (err) {
      console.warn(`arXiv search failed for topic "${topic}":`, err);
    }
    // be polite to arXiv's API (recommended: no more than 1 req/3s)
    await new Promise((r) => setTimeout(r, 3000));
  }
  return [...seen.values()];
}
