import { config } from "./config.js";
import { listProjectPages, readFile } from "./lapisClient.js";
import { extractProjectContexts, type ProjectContext } from "./relevance.js";

export async function loadProjectContexts(): Promise<ProjectContext[]> {
  const files = await listProjectPages(config.lapis.projectsPrefix);
  const contents = await Promise.all(
    files.map(async (f) => ({ path: f.path, content: (await readFile(f.path)) ?? "" }))
  );
  if (contents.length === 0) return [];
  return extractProjectContexts(contents);
}
