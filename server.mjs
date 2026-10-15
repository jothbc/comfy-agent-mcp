
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PROJECT_DIR = dirname(fileURLToPath(import.meta.url));
const PYTHON_CLI = join(PROJECT_DIR, "comfy_cli.py");

const server = new McpServer({
  name: "comfyui-local",
  version: "1.0.0",
});

function runCli(args) {
  return new Promise((resolve, reject) => {
    const child = spawn("py", [PYTHON_CLI, ...args, "--json"], {
      cwd: PROJECT_DIR,
      shell: false,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");

    child.stdout.on("data", (data) => {
      stdout += data;
    });

    child.stderr.on("data", (data) => {
      stderr += data;
    });

    child.on("error", reject);

    child.on("close", (code) => {
      if (code !== 0) {
        let message = stderr || `CLI exited with code ${code}`;
        try {
          message = JSON.parse(stdout).error ?? message;
        } catch {
          // stdout is not JSON; keep stderr
        }
        reject(new Error(message));
        return;
      }

      try {
        resolve(JSON.parse(stdout));
      } catch {
        reject(new Error(`Invalid JSON from CLI: ${stdout}`));
      }
    });
  });
}

const aspectRatios = {
  "1:1": "1:1 (Square)",
  "16:9": "16:9 (Widescreen)",
  "9:16": "9:16 (Portrait)",
};

// Stays under the 60s default MCP client request timeout.
const MAX_WAIT_SECONDS = 50;

const QUEUED_NOTE =
  "Jobs are queued, not finished. Call get_result with these prompt_ids " +
  "to receive the image paths.";

async function queueJobs(args) {
  const result = await runCli([...args, "--no-wait"]);

  if (!result.success) {
    throw new Error(JSON.stringify(result));
  }

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            ...result,
            prompt_ids: result.results.map((item) => item.prompt_id),
            note: QUEUED_NOTE,
          },
          null,
          2,
        ),
      },
    ],
  };
}

server.registerTool(
  "generate_image",
  {
    title: "Generate Image",
    description:
      "Generate one or more images locally using ComfyUI and Qwen Image 2.1. " +
      "Queues the job(s) and returns immediately with prompt_ids and seeds; " +
      "then call get_result with the prompt_ids to wait for and receive the image paths. " +
      "The seed is a decimal string; " +
      "pass that exact string back to reproduce the same generation. " +
      "A seed does not edit an existing picture; use edit_image for that. " +
      "Prefer width+height for exact sizes (icons ~256-512, UI ~1024, cinematic up to 2048). " +
      "Values are rounded to multiples of 8. " +
      "If width/height are omitted, aspect_ratio + megapixels are used instead.",
    inputSchema: {
      prompt: z.string().min(1).describe("Detailed image description"),
      count: z.number().int().min(1).max(4).default(1)
        .describe("Number of images to generate, from 1 to 4"),
      aspect_ratio: z.enum(["1:1", "16:9", "9:16"]).default("1:1")
        .describe("Image aspect ratio (used when width/height are omitted)"),
      megapixels: z.number().min(0.05).max(4).default(1)
        .describe(
          "Approximate image resolution in megapixels " +
          "(used when width/height are omitted; min 0.05, max 4)"
        ),
      width: z.number().int().min(64).max(2048).optional()
        .describe(
          "Exact width in pixels (64-2048). Requires height. " +
          "When set with height, bypasses aspect_ratio/megapixels."
        ),
      height: z.number().int().min(64).max(2048).optional()
        .describe(
          "Exact height in pixels (64-2048). Requires width. " +
          "When set with width, bypasses aspect_ratio/megapixels."
        ),
      seed: z.string().regex(/^\d+$/).optional()
        .describe(
          "Seed as a decimal string. Reuse the exact seed from a previous result " +
          "to reproduce that generation. When count is greater than 1, later images use seed+1, seed+2, and so on."
        ),
    },
  },
  async ({ prompt, count, aspect_ratio, megapixels, width, height, seed }) => {
    const hasWidth = width !== undefined && width !== null;
    const hasHeight = height !== undefined && height !== null;

    if (hasWidth !== hasHeight) {
      throw new Error("width and height must both be provided together");
    }

    const args = [
      "--prompt", prompt,
      "--count", String(count),
    ];

    if (hasWidth && hasHeight) {
      args.push("--width", String(width), "--height", String(height));
    } else {
      args.push(
        "--aspect-ratio", aspectRatios[aspect_ratio],
        "--megapixels", String(megapixels),
      );
    }

    if (seed) {
      args.push("--seed", seed);
    }

    return queueJobs(args);
  }
);

server.registerTool(
  "edit_image",
  {
    title: "Edit Image",
    description:
      "Edit an existing local image with Qwen Image 2.1. " +
      "Pass the file path and an instruction such as 'remove the Chinese text and keep the rest unchanged'. " +
      "The source image is used as a reference, so the result stays close to the original. " +
      "Reusing a generation seed does not edit a picture; use this tool instead. " +
      "Queues the job(s) and returns immediately with prompt_ids and seeds; " +
      "then call get_result with the prompt_ids to wait for and receive the image paths. " +
      "The returned seed is a decimal string; pass it back exactly to reproduce the same edit.",
    inputSchema: {
      image_path: z.string().min(1)
        .describe("Absolute or project path to the PNG or JPEG to edit"),
      prompt: z.string().min(1)
        .describe("Edit instruction describing what to change and what to keep"),
      count: z.number().int().min(1).max(4).default(1)
        .describe("Number of edit variations to generate, from 1 to 4"),
      seed: z.string().regex(/^\d+$/).optional()
        .describe(
          "Seed as a decimal string. Reuse the exact seed from a previous edit to reproduce it."
        ),
      negative_prompt: z.string().optional()
        .describe("Optional things to avoid, such as text, letters, or watermarks"),
    },
  },
  async ({ image_path, prompt, count, seed, negative_prompt }) => {
    const args = [
      "--prompt", prompt,
      "--image", image_path,
      "--count", String(count),
    ];

    if (seed) {
      args.push("--seed", seed);
    }

    if (negative_prompt) {
      args.push("--negative", negative_prompt);
    }

    return queueJobs(args);
  }
);

server.registerTool(
  "get_result",
  {
    title: "Get Image Result",
    description:
      "Wait for images queued by generate_image or edit_image and return their file paths. " +
      `Waits up to wait_seconds (max ${MAX_WAIT_SECONDS}). ` +
      "Each result has a status: completed (images ready), running, queued, error, or not_found. " +
      "If done is false, some jobs are still generating: call get_result again with the same prompt_ids " +
      "and keep calling until done is true. Slow machines can need several calls.",
    inputSchema: {
      prompt_ids: z.array(z.string().min(1)).min(1).max(16)
        .describe("prompt_ids returned by generate_image or edit_image"),
      wait_seconds: z.number().min(0).max(MAX_WAIT_SECONDS).default(45)
        .describe(`Max seconds to wait before reporting pending jobs (0-${MAX_WAIT_SECONDS})`),
    },
  },
  async ({ prompt_ids, wait_seconds }) => {
    const result = await runCli([
      "--result", ...prompt_ids,
      "--wait-seconds", String(wait_seconds),
    ]);

    if (!result.done) {
      result.note =
        "Some jobs are still generating. Call get_result again with the same prompt_ids.";
    }

    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(result, null, 2),
        },
      ],
      isError: !result.success,
    };
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);
