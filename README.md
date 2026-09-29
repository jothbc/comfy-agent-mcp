# comfy-agent-mcp

Local MCP server and CLI that drive [ComfyUI](https://github.com/comfyanonymous/ComfyUI) with **Qwen Image 2.1** so Cursor (or any MCP client) can generate and edit images on your machine.

## Features

- **`generate_image`** — text-to-image with aspect ratio, megapixels, or exact `width`/`height` (64–2048)
- **`edit_image`** — edit an existing PNG/JPEG with an instruction (e.g. “remove Chinese text, keep the rest”)
- Seed reuse as a decimal string for reproducible runs
- Same capabilities available from the Python CLI (`comfy_cli.py`)

## Requirements

- [Node.js](https://nodejs.org/) 18+
- Python 3.10+ with `requests` (`py -m pip install requests`)
- ComfyUI running locally at `http://127.0.0.1:8188`
- Qwen Image 2.1 models loaded in ComfyUI (matching `workflow.json`)

## Setup

```bash
npm install
py -m pip install requests
```

Start ComfyUI, then run the MCP server:

```bash
npm start
```

### Cursor MCP config

Point Cursor at `server.mjs` (adjust the path to your clone):

```json
{
  "mcpServers": {
    "comfyui-local": {
      "command": "node",
      "args": ["E:/path/to/comfy-agent/server.mjs"]
    }
  }
}
```

On Windows the CLI is invoked with `py`. Ensure the Python launcher is on your `PATH`.

## MCP tools

### `generate_image`

| Parameter | Description |
|-----------|-------------|
| `prompt` | Image description |
| `count` | 1–4 images (default 1) |
| `aspect_ratio` | `1:1`, `16:9`, or `9:16` (used if size not set) |
| `megapixels` | ~0.05–4 (used if size not set) |
| `width` / `height` | Exact pixels 64–2048 (both required; rounded to multiples of 8) |
| `seed` | Decimal string from a previous result |

Size tips: icons ~256–512, UI ~1024, cinematic up to 2048.

### `edit_image`

| Parameter | Description |
|-----------|-------------|
| `image_path` | Path to the source PNG/JPEG |
| `prompt` | Edit instruction |
| `count` | 1–4 variations |
| `seed` | Optional decimal string |
| `negative_prompt` | Optional (e.g. text, watermarks) |

Editing uses the source as a Qwen reference. Reusing a generation seed alone does **not** edit a picture — use `edit_image`.

## CLI

```bash
# Generate
py comfy_cli.py --prompt "cyberpunk city at night" --aspect-ratio "16:9 (Widescreen)" --megapixels 1

# Exact size
py comfy_cli.py --prompt "app icon, flat" --width 256 --height 256

# Edit
py comfy_cli.py --image ./output/photo.png --prompt "Remove all overlaid text" --negative "text, letters"

# Machine-readable output
py comfy_cli.py --prompt "..." --json
```

Images are written to `output/` by default (gitignored).

## Project layout

| File | Role |
|------|------|
| `server.mjs` | MCP server (stdio) |
| `comfy_cli.py` | ComfyUI HTTP client / CLI |
| `workflow.json` | Qwen Image 2.1 workflow graph |

## License

MIT — see [LICENSE](LICENSE).
