import { config } from "./config.js";
import { overwritePage } from "./lapisClient.js";
import type { CandidateItem } from "./candidates.js";

export type ResearchDepth = "light" | "deep";

export interface DigestFinding {
  candidate: CandidateItem;
  topic: string;
  depth: ResearchDepth;
}

export function formatFindingLine(finding: DigestFinding): string {
  const { candidate, topic, depth } = finding;
  const id = candidate.id || candidate.url;
  return `- [${id}] **[${candidate.title}](${candidate.url})** | topic: ${topic} | depth: ${depth}`;
}

export function buildDigestContent(dateStr: string, findings: DigestFinding[]): string {
  const lines: string[] = [
    "---",
    `id: digest-${dateStr}`,
    "type: digest",
    `date: ${dateStr}`,
    "---",
    "",
    `# Research Digest (${dateStr})`,
    "",
    "## Findings",
  ];

  if (findings.length === 0) {
    lines.push("_No new findings recorded for this run._");
  } else {
    for (const finding of findings) {
      lines.push(formatFindingLine(finding));
    }
  }

  return lines.join("\n") + "\n";
}

/**
 * Writes findings to research/digests/<date>.md.
 * Only writes if config.enableDigestWriter is true.
 */
export async function writeResearchDigest(
  findings: DigestFinding[],
  customDate?: string
): Promise<string | null> {
  if (!config.enableDigestWriter) {
    return null;
  }

  const dateStr = customDate ?? new Date().toISOString().slice(0, 10);
  const normalizedPrefix = config.lapis.digestPrefix.replace(/\/$/, "");
  const targetPath = `${normalizedPrefix}/${dateStr}.md`;

  const content = buildDigestContent(dateStr, findings);
  await overwritePage(targetPath, content);
  console.log(`[digestWriter] Wrote ${findings.length} findings to ${targetPath}`);

  return targetPath;
}
