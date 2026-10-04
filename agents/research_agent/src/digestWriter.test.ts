import { buildDigestContent, formatFindingLine, type DigestFinding } from "./digestWriter.js";

function runTests() {
  console.log("Running digestWriter tests...");

  const mockFinding: DigestFinding = {
    candidate: {
      kind: "paper",
      source: "arxiv",
      id: "arxiv:2401.05432",
      title: "FlashAttention-3: Fast and Accurate Attention",
      summary: "Abstract content...",
      url: "https://arxiv.org/abs/2401.05432",
    },
    topic: "Efficient Transformers",
    depth: "deep",
  };

  // Test 1: Verify single line format
  const line = formatFindingLine(mockFinding);
  const expectedLine =
    "- [arxiv:2401.05432] **[FlashAttention-3: Fast and Accurate Attention](https://arxiv.org/abs/2401.05432)** | topic: Efficient Transformers | depth: deep";
  if (line !== expectedLine) {
    throw new Error(`Test 1 Failed:\nExpected: ${expectedLine}\nReceived: ${line}`);
  }
  console.log("✓ formatFindingLine matches spec");

  // Test 2: Verify frontmatter and markdown structure
  const markdown = buildDigestContent("2026-09-25", [mockFinding]);
  if (!markdown.includes("id: digest-2026-09-25") || !markdown.includes("type: digest")) {
    throw new Error("Test 2 Failed: Frontmatter missing required metadata keys");
  }
  if (!markdown.includes(expectedLine)) {
    throw new Error("Test 2 Failed: Digest body missing formatted finding");
  }
  console.log("✓ buildDigestContent frontmatter & structure verified");

  console.log("All unit tests passed!");
}

runTests();
