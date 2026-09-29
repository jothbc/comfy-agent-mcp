import argparse
import copy
import json
import math
import random
import struct
import sys
import time
from pathlib import Path

import requests


COMFY_URL = "http://127.0.0.1:8188"
PROJECT_DIR = Path(__file__).resolve().parent
MIN_DIMENSION = 64
MAX_DIMENSION = 2048
DIMENSION_MULTIPLE = 8


def read_image_size(path):
    """Read width and height from a PNG or JPEG without extra dependencies."""
    data = path.read_bytes()

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24:
            raise ValueError(f"Invalid PNG: {path}")
        width, height = struct.unpack(">II", data[16:24])
        return width, height

    if data.startswith(b"\xff\xd8"):
        return _read_jpeg_size(data, path)

    raise ValueError(
        f"Unsupported image type for {path.name}. Use PNG or JPEG."
    )


def _read_jpeg_size(data, path):
    index = 2
    while index + 8 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue

        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return width, height

        if marker in (0xD8, 0xD9):
            index += 2
            continue

        if index + 4 > len(data):
            break

        segment_length = struct.unpack(">H", data[index + 2:index + 4])[0]
        index += 2 + segment_length

    raise ValueError(f"Could not read JPEG size: {path}")


def reference_resolution(width, height):
    """Keep native size (0) unless a side exceeds MAX_DIMENSION."""
    if max(width, height) <= MAX_DIMENSION:
        return 0

    ratio = width / height
    if width >= height:
        resolution = MAX_DIMENSION / math.sqrt(ratio)
    else:
        resolution = MAX_DIMENSION * math.sqrt(ratio)

    snapped = int(round(resolution / 32) * 32)
    return max(32, min(4096, snapped))


def upload_image(path):
    """Upload a local file into the ComfyUI input folder."""
    with open(path, "rb") as handle:
        response = requests.post(
            f"{COMFY_URL}/upload/image",
            files={"image": (path.name, handle, "application/octet-stream")},
            data={"overwrite": "true", "type": "input"},
            timeout=120,
        )

    response.raise_for_status()
    payload = response.json()
    name = payload["name"]
    subfolder = payload.get("subfolder") or ""
    if subfolder:
        return f"{subfolder}/{name}"
    return name


def apply_reference_edit(workflow, uploaded_name, source_width, source_height):
    """Wire the source image into Qwen Image 2.1 as an edit reference."""
    workflow["load_ref"] = {
        "inputs": {
            "image": uploaded_name,
        },
        "class_type": "LoadImage",
    }

    prompt_node = workflow["459:452"]
    prompt_node["inputs"]["vae"] = ["459:454", 0]
    prompt_node["inputs"]["images.image_1"] = ["load_ref", 0]
    prompt_node["inputs"]["resolution"] = reference_resolution(
        source_width,
        source_height,
    )

    # Latent must match the reference size or the edit shifts.
    workflow["459:458"]["inputs"]["latent_image"] = ["459:452", 2]


def round_dimension(value):
    """Round to nearest multiple of DIMENSION_MULTIPLE, clamped to limits."""
    rounded = int(round(value / DIMENSION_MULTIPLE) * DIMENSION_MULTIPLE)
    return max(MIN_DIMENSION, min(MAX_DIMENSION, rounded))


def normalize_dimensions(width, height):
    """Validate and normalize width/height. Both required together."""
    if width is None and height is None:
        return None, None

    if width is None or height is None:
        raise ValueError(
            "width and height must both be provided together"
        )

    if not (MIN_DIMENSION <= width <= MAX_DIMENSION):
        raise ValueError(
            f"width must be between {MIN_DIMENSION} and {MAX_DIMENSION}"
        )

    if not (MIN_DIMENSION <= height <= MAX_DIMENSION):
        raise ValueError(
            f"height must be between {MIN_DIMENSION} and {MAX_DIMENSION}"
        )

    return round_dimension(width), round_dimension(height)


def load_workflow(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def queue_prompt(workflow):
    response = requests.post(
        f"{COMFY_URL}/prompt",
        json={"prompt": workflow},
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    if "error" in data:
        raise RuntimeError(
            f"ComfyUI rejeitou o workflow:\n"
            f"{json.dumps(data, indent=2)}"
        )

    return data["prompt_id"]


def wait_for_completion(prompt_id, quiet=False):
    if not quiet:
        print(f"Prompt enviado: {prompt_id}")

    while True:
        response = requests.get(
            f"{COMFY_URL}/history/{prompt_id}",
            timeout=30,
        )

        response.raise_for_status()

        history = response.json()

        if prompt_id in history:
            return history[prompt_id]

        if not quiet:
            print(".", end="", flush=True)

        time.sleep(1)


def extract_images(history):
    images = []

    outputs = history.get("outputs", {})

    for node_output in outputs.values():
        for image in node_output.get("images", []):
            images.append(image)

    return images


def download_image(image_info, output_dir):
    filename = image_info["filename"]
    subfolder = image_info.get("subfolder", "")
    image_type = image_info.get("type", "output")

    response = requests.get(
        f"{COMFY_URL}/view",
        params={
            "filename": filename,
            "subfolder": subfolder,
            "type": image_type,
        },
        timeout=120,
    )

    response.raise_for_status()

    output_dir.mkdir(parents=True, exist_ok=True)

    destination = output_dir / filename

    with open(destination, "wb") as f:
        f.write(response.content)

    return destination.resolve().as_posix()


def generate_one(
    workflow_path,
    prompt,
    negative_prompt=None,
    seed=None,
    steps=None,
    cfg=None,
    aspect_ratio=None,
    megapixels=None,
    width=None,
    height=None,
    reference_image=None,
    reference_size=None,
    filename_prefix=None,
    output_dir=PROJECT_DIR / "output",
    quiet=False,
):
    workflow = load_workflow(workflow_path)
    workflow = copy.deepcopy(workflow)

    # -------------------------
    # Prompt
    # -------------------------

    prompt_node = workflow["459:452"]

    prompt_node["inputs"]["prompt"] = prompt

    if negative_prompt is not None:
        prompt_node["inputs"]["negative_prompt"] = negative_prompt

    # -------------------------
    # Resolution
    # -------------------------

    if reference_image is not None:
        source_width, source_height = reference_size
        apply_reference_edit(
            workflow,
            reference_image,
            source_width,
            source_height,
        )
        if filename_prefix is None:
            filename_prefix = "Qwen_image_2.1_edit"
    else:
        width, height = normalize_dimensions(width, height)

        if width is not None and height is not None:
            # Bypass ResolutionSelector; set EmptyLatentImage directly
            latent_node = workflow["459:456"]
            latent_node["inputs"]["width"] = width
            latent_node["inputs"]["height"] = height
        else:
            resolution_node = workflow["13"]

            if aspect_ratio is not None:
                resolution_node["inputs"]["aspect_ratio"] = aspect_ratio

            if megapixels is not None:
                resolution_node["inputs"]["megapixels"] = megapixels

    # -------------------------
    # Sampler
    # -------------------------

    sampler_node = workflow["459:458"]

    if seed is None:
        seed = random.randint(0, 2**63 - 1)

    sampler_node["inputs"]["seed"] = seed

    if steps is not None:
        sampler_node["inputs"]["steps"] = steps

    if cfg is not None:
        sampler_node["inputs"]["cfg"] = cfg

    # -------------------------
    # Filename
    # -------------------------

    if filename_prefix is not None:
        workflow["461"]["inputs"]["filename_prefix"] = filename_prefix

    # -------------------------
    # Execute
    # -------------------------

    prompt_id = queue_prompt(workflow)

    history = wait_for_completion(
        prompt_id,
        quiet=quiet,
    )

    status = history.get("status", {})
    if status.get("status_str") == "error":
        raise RuntimeError(
            json.dumps(status.get("messages", status), ensure_ascii=False)
        )

    images = extract_images(history)

    downloaded = []

    for image in images:
        path = download_image(
            image,
            output_dir,
        )

        downloaded.append(str(path))

    return {
        "prompt_id": prompt_id,
        "seed": seed,
        "images": downloaded,
    }


def generate(
    workflow_path,
    prompt,
    negative_prompt=None,
    seed=None,
    steps=None,
    cfg=None,
    aspect_ratio=None,
    megapixels=None,
    width=None,
    height=None,
    image_path=None,
    filename_prefix=None,
    count=1,
    output_dir=PROJECT_DIR / "output",
    quiet=False,
):
    reference_image = None
    reference_size = None

    if image_path is not None:
        source = Path(image_path)
        if not source.is_file():
            raise FileNotFoundError(f"Image not found: {source}")
        reference_size = read_image_size(source)
        reference_image = upload_image(source)

    results = []

    for index in range(count):
        # Se o usuário informou uma seed,
        # usamos seeds diferentes para cada geração.
        if seed is not None:
            current_seed = seed + index
        else:
            current_seed = random.randint(
                0,
                2**63 - 1,
            )

        if not quiet:
            print()
            print(
                f"[{index + 1}/{count}] "
                f"Gerando imagem..."
            )
            print(f"Seed: {current_seed}")

        try:
            result = generate_one(
                workflow_path=workflow_path,
                prompt=prompt,
                negative_prompt=negative_prompt,
                seed=current_seed,
                steps=steps,
                cfg=cfg,
                aspect_ratio=aspect_ratio,
                megapixels=megapixels,
                width=width,
                height=height,
                reference_image=reference_image,
                reference_size=reference_size,
                filename_prefix=filename_prefix,
                output_dir=output_dir,
                quiet=quiet,
            )

            results.append({
                "success": True,
                "seed": str(current_seed),
                "prompt_id": result["prompt_id"],
                "images": result["images"],
            })

            if not quiet:
                for image in result["images"]:
                    print(f"✓ {image}")

        except Exception as e:
            results.append({
                "success": False,
                "seed": str(current_seed),
                "error": str(e),
            })

            if not quiet:
                print(f"✗ Erro: {e}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="ComfyUI local image generation CLI"
    )

    parser.add_argument(
        "--workflow",
        default="workflow.json",
    )

    parser.add_argument(
        "--prompt",
        required=True,
    )

    parser.add_argument(
        "--negative",
        default=None,
    )

    parser.add_argument(
        "--seed",
        type=int,
    )

    parser.add_argument(
        "--steps",
        type=int,
    )

    parser.add_argument(
        "--cfg",
        type=float,
    )

    parser.add_argument(
        "--aspect-ratio",
    )

    parser.add_argument(
        "--megapixels",
        type=float,
    )

    parser.add_argument(
        "--width",
        type=int,
        help=(
            f"Exact width in pixels ({MIN_DIMENSION}-{MAX_DIMENSION}). "
            "Requires --height. Bypasses aspect-ratio/megapixels."
        ),
    )

    parser.add_argument(
        "--height",
        type=int,
        help=(
            f"Exact height in pixels ({MIN_DIMENSION}-{MAX_DIMENSION}). "
            "Requires --width. Bypasses aspect-ratio/megapixels."
        ),
    )

    parser.add_argument(
        "--image",
        help=(
            "Source PNG or JPEG to edit. The prompt becomes an edit "
            "instruction and the image is used as a Qwen reference."
        ),
    )

    parser.add_argument(
        "--filename",
    )

    parser.add_argument(
        "--count",
        type=int,
        default=1,
    )

    parser.add_argument(
        "--output",
        default=str(PROJECT_DIR / "output"),
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Retorna resultado em JSON",
    )

    args = parser.parse_args()

    if args.count < 1:
        print("ERRO: --count precisa ser >= 1")
        sys.exit(1)

    try:
        # Validate dimensions early so JSON mode still returns an error object
        normalize_dimensions(args.width, args.height)

        results = generate(
            workflow_path=args.workflow,
            prompt=args.prompt,
            negative_prompt=args.negative,
            seed=args.seed,
            steps=args.steps,
            cfg=args.cfg,
            aspect_ratio=args.aspect_ratio,
            megapixels=args.megapixels,
            width=args.width,
            height=args.height,
            image_path=args.image,
            filename_prefix=args.filename,
            count=args.count,
            output_dir=Path(args.output),
            quiet=args.json,
        )

        response = {
            "success": all(
                result["success"]
                for result in results
            ),
            "count": len(results),
            "results": results,
        }

        if args.json:
            print(json.dumps(
                response,
                indent=2,
                ensure_ascii=False,
            ))

    except requests.exceptions.ConnectionError:
        response = {
            "success": False,
            "error": (
                "Não foi possível conectar ao "
                f"ComfyUI em {COMFY_URL}"
            ),
        }

        if args.json:
            print(json.dumps(response, indent=2))
        else:
            print(response["error"])

        sys.exit(1)

    except Exception as e:
        response = {
            "success": False,
            "error": str(e),
        }

        if args.json:
            print(json.dumps(response, indent=2))
        else:
            print(f"ERRO: {e}")

        sys.exit(1)


if __name__ == "__main__":
    main()