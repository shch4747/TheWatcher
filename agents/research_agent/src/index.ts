import { runWhatsappJob } from "./jobs/whatsapp.js";
import { runProjectsJob } from "./jobs/projects.js";
import { runTrendingJob } from "./jobs/trending.js";
import { loadState, periodicDue } from "./state.js";

type Job = "whatsapp" | "projects" | "trending" | "periodic" | "all";

function parseJob(): Job {
  const arg = process.argv.find((a) => a.startsWith("--job="));
  const job = (arg?.split("=")[1] ?? "all") as Job;
  if (!["whatsapp", "projects", "trending", "periodic", "all"].includes(job)) {
    throw new Error(`Unknown --job value: ${job}`);
  }
  return job;
}

async function main() {
  const job = parseJob();
  console.log(`Running job: ${job}`);

  if (job === "whatsapp" || job === "all") {
    await runWhatsappJob();
  }

  if (job === "projects" || job === "periodic" || job === "all") {
    // Re-load state fresh right before the due-check. Job 1 (if it ran
    // above) never touches lastProjectsRunAt, so this is unaffected by it.
    const state = await loadState();
    if (periodicDue(state.lastProjectsRunAt)) {
      await runProjectsJob();
    } else {
      console.log("[job:projects] Not due yet — skipping.");
    }
  }

  if (job === "trending" || job === "periodic" || job === "all") {
    const state = await loadState();
    if (periodicDue(state.lastTrendingRunAt)) {
      await runTrendingJob();
    } else {
      console.log("[job:trending] Not due yet — skipping.");
    }
  }

  console.log("Done.");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
