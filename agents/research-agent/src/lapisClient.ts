import { promises as fs } from "node:fs";
import path from "node:path";
import { config } from "./config.js";

export interface ManifestEntry {
  path: string;
  [key: string]: unknown;
}

function authHeaders(): Record<string, string> {
  if (config.lapis.bearerToken) {
    return { Authorization: `Bearer ${config.lapis.bearerToken}` };
  }
  return { Cookie: config.lapis.sessionCookie };
}

function encodeVaultPath(vaultPath: string): string {
  return vaultPath
    .split("/")
    .map((seg) => encodeURIComponent(seg))
    .join("/");
}

// --- Local filesystem backend (LOCAL_MODE=true) -----------------------
// Mirrors vault paths 1:1 as files under config.localOutputDir, so
// "Trending Feed.md" becomes "<localOutputDir>/Trending Feed.md" and
// "Projects/Foo.md" becomes "<localOutputDir>/Projects/Foo.md". This lets
// every other module (state.ts, pages.ts, jobs/*) work identically whether
// talking to Lapis or to a folder on disk.

function localFsPath(vaultPath: string): string {
  return path.join(config.localOutputDir, vaultPath);
}

async function localReadFile(vaultPath: string): Promise<string | null> {
  try {
    return await fs.readFile(localFsPath(vaultPath), "utf8");
  } catch (err: any) {
    if (err?.code === "ENOENT") return null;
    throw err;
  }
}

async function localWriteFile(vaultPath: string, content: string): Promise<void> {
  const fullPath = localFsPath(vaultPath);
  await fs.mkdir(path.dirname(fullPath), { recursive: true });
  await fs.writeFile(fullPath, content, "utf8");
}

async function walkDir(dir: string, base: string): Promise<ManifestEntry[]> {
  let entries: import("node:fs").Dirent[];
  try {
    entries = await fs.readdir(dir, { withFileTypes: true });
  } catch (err: any) {
    if (err?.code === "ENOENT") return [];
    throw err;
  }
  const out: ManifestEntry[] = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    const rel = path.relative(base, full).split(path.sep).join("/");
    if (entry.isDirectory()) {
      out.push(...(await walkDir(full, base)));
    } else {
      out.push({ path: rel });
    }
  }
  return out;
}

async function localGetManifest(): Promise<ManifestEntry[]> {
  return walkDir(config.localOutputDir, config.localOutputDir);
}

// --- Public API (dispatches to Lapis or local backend) -----------------

export async function getManifest(): Promise<ManifestEntry[]> {
  if (config.localMode) return localGetManifest();

  const url = `${config.lapis.baseUrl}/api/vaults/${config.lapis.vaultId}/manifest`;
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) throw new Error(`Lapis manifest request failed: ${res.status}`);
  const data = (await res.json()) as ManifestEntry[] | { files: ManifestEntry[] };
  return Array.isArray(data) ? data : data.files;
}

/** Read a file by vault path. Returns null if it doesn't exist. */
export async function readFile(vaultPath: string): Promise<string | null> {
  if (config.localMode) return localReadFile(vaultPath);

  const url = `${config.lapis.baseUrl}/api/vaults/${config.lapis.vaultId}/files/${encodeVaultPath(
    vaultPath
  )}`;
  const res = await fetch(url, { headers: authHeaders() });
  if (res.status === 404) return null;
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`Lapis API GET files/${vaultPath} failed: ${res.status} ${body}`);
  }
  return res.text();
}

export async function writeFile(vaultPath: string, content: string): Promise<void> {
  if (config.dryRun) {
    console.log(
      `[DRY_RUN] Would write ${content.length} chars to "${vaultPath}"${
        config.localMode ? ` (local: ${localFsPath(vaultPath)})` : ""
      }:\n` +
        content
          .split("\n")
          .map((l) => `    | ${l}`)
          .join("\n")
    );
    return;
  }

  if (config.localMode) {
    await localWriteFile(vaultPath, content);
    return;
  }

  const url = `${config.lapis.baseUrl}/api/vaults/${config.lapis.vaultId}/files/${encodeVaultPath(
    vaultPath
  )}`;
  const res = await fetch(url, {
    method: "PUT",
    headers: { ...authHeaders(), "Content-Type": "text/markdown" },
    body: content,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`Lapis API PUT files/${vaultPath} failed: ${res.status} ${body}`);
  }
}

export async function listProjectPages(prefix: string): Promise<ManifestEntry[]> {
  const manifest = await getManifest();
  return manifest.filter(
    (f) => f.path.startsWith(prefix) && f.path.endsWith(".md") && !f.path.split("/").pop()!.startsWith(".")
  );
}

/**
 * Appends items under a `## {heading}` section in the note at `path`,
 * creating the note and/or the section if they don't exist yet. Items whose
 * `dedupeKey` substring is already present anywhere in the note are skipped,
 * so repeated runs don't re-add the same paper/article. Returns exactly the
 * items that were actually written, so callers can log precisely what was
 * newly pushed (as opposed to what was attempted).
 *
 * This is intentionally a dumb text operation (not a markdown AST edit) to
 * stay resilient to whatever structure members' notes already have — it
 * only ever adds a section near the top, never rewrites existing content.
 */
export async function appendUnderHeading<T extends { line: string; dedupeKey: string }>(
  path: string,
  heading: string,
  items: T[],
  preamble?: string
): Promise<{ added: T[] }> {
  if (items.length === 0) return { added: [] };

  const existing = (await readFile(path)) ?? "";
  const toAdd = items.filter((i) => !existing.includes(i.dedupeKey));
  if (toAdd.length === 0) return { added: [] };

  const headingMarker = `## ${heading}`;
  const newLines = toAdd.map((i) => i.line).join("\n");

  let updated: string;
  if (existing.includes(headingMarker)) {
    // Insert right after the heading line (and any immediately-following
    // blank line), so newest items land at the top of the section.
    const idx = existing.indexOf(headingMarker);
    const afterHeading = idx + headingMarker.length;
    const rest = existing.slice(afterHeading);
    const blankMatch = rest.match(/^\r?\n(\r?\n)?/);
    const insertAt = afterHeading + (blankMatch ? blankMatch[0].length : 0);
    updated = existing.slice(0, insertAt) + newLines + "\n" + existing.slice(insertAt);
  } else {
    const sep = existing.trim().length > 0 ? "\n\n" : "";
    updated = `${existing}${sep}${headingMarker}\n\n${preamble ? preamble + "\n\n" : ""}${newLines}\n`;
  }

  await writeFile(path, updated);
  return { added: toAdd };
}

/** Overwrites a page entirely — used for the trending feed, which is a
 * point-in-time snapshot rather than an append-only log. */
export async function overwritePage(path: string, content: string): Promise<void> {
  await writeFile(path, content);
}

export async function appendLogLine(path: string, line: string): Promise<void> {
  await appendLogLines(path, [line]);
}

/** Appends several log lines in a single read+write, so a job that pushes
 * N items to N destinations doesn't make N separate API round-trips just
 * to log them. */
export async function appendLogLines(path: string, lines: string[]): Promise<void> {
  if (lines.length === 0) return;
  const existing = (await readFile(path)) ?? "# Research Agent Log\n\n";
  await writeFile(path, `${existing.trimEnd()}\n${lines.join("\n")}\n`);
}
