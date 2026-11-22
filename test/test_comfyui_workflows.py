# -*- coding: utf-8 -*-
"""Tests for ComfyUI workflow discovery, UI->API conversion, and prompt injection."""
import asyncio
import json
import unittest

import httpx

from backend.picturesys import comfyui


SAMPLE_OBJECT_INFO = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["modelA.safetensors", "modelB.safetensors"]]}},
        "input_order": {"required": ["ckpt_name"]},
    },
    "UNETLoader": {
        "input": {
            "required": {
                "unet_name": [["unetA.safetensors"]],
                "weight_dtype": [["default", "fp8_e4m3fn"]],
            }
        },
        "input_order": {"required": ["unet_name", "weight_dtype"]},
    },
    "LoraLoader": {
        "input": {
            "required": {
                "model": ["MODEL"],
                "clip": ["CLIP"],
                "lora_name": [["loraA.safetensors"]],
                "strength_model": ["FLOAT", {"default": 1.0}],
                "strength_clip": ["FLOAT", {"default": 1.0}],
            }
        },
        "input_order": {"required": ["model", "clip", "lora_name", "strength_model", "strength_clip"]},
    },
    "CLIPTextEncode": {
        "input": {"required": {"text": ["STRING", {"multiline": True}], "clip": ["CLIP"]}},
        "input_order": {"required": ["text", "clip"]},
    },
    "TextEncodeQwenImage21": {
        "input": {
            "required": {
                "clip": ["CLIP"],
                "prompt": ["STRING", {"multiline": True}],
                "negative_prompt": ["STRING", {"multiline": True}],
                "resolution": ["INT", {"default": 1024}],
                "images": ["COMFY_AUTOGROW_V3", {}],
            },
            "optional": {"vae": ["VAE"]},
        },
        "input_order": {
            "required": ["clip", "prompt", "negative_prompt", "resolution", "images"],
            "optional": ["vae"],
        },
    },
    "EmptyLatentImage": {
        "input": {
            "required": {
                "width": ["INT", {"default": 512}],
                "height": ["INT", {"default": 512}],
                "batch_size": ["INT", {"default": 1}],
            }
        },
        "input_order": {"required": ["width", "height", "batch_size"]},
    },
    "KSampler": {
        "input": {
            "required": {
                "model": ["MODEL"],
                "seed": ["INT", {"default": 0, "control_after_generate": True}],
                "steps": ["INT", {"default": 20}],
                "cfg": ["FLOAT", {"default": 8.0}],
                "sampler_name": [["euler", "er_sde"]],
                "scheduler": [["normal", "simple", "karras"]],
                "positive": ["CONDITIONING"],
                "negative": ["CONDITIONING"],
                "latent_image": ["LATENT"],
                "denoise": ["FLOAT", {"default": 1.0}],
            }
        },
        "input_order": {
            "required": [
                "model",
                "seed",
                "steps",
                "cfg",
                "sampler_name",
                "scheduler",
                "positive",
                "negative",
                "latent_image",
                "denoise",
            ]
        },
    },
    "VAEDecode": {
        "input": {"required": {"samples": ["LATENT"], "vae": ["VAE"]}},
        "input_order": {"required": ["samples", "vae"]},
    },
    "LoadImage": {
        "input": {"required": {"image": [["ref.png"], {"image_upload": True}]}},
        "input_order": {"required": ["image"]},
    },
    "SaveImage": {
        "input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {"default": "ComfyUI"}]}},
        "input_order": {"required": ["images", "filename_prefix"]},
    },
    "SaveVideo": {
        "input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING", {}]}},
        "input_order": {"required": ["video", "filename_prefix"]},
    },
}


class ComfyUIWorkflowTests(unittest.TestCase):
    def test_ui_to_api_positional_widgets_and_links(self):
        ui = {
            "nodes": [
                {"id": 1, "type": "CheckpointLoaderSimple", "mode": 0, "inputs": [], "widgets_values": ["modelA.safetensors"]},
                # Negative node has a smaller id than positive node on purpose
                {"id": 2, "type": "CLIPTextEncode", "mode": 0, "inputs": [{"name": "clip", "type": "CLIP", "link": 1}], "widgets_values": ["old neg"]},
                {"id": 3, "type": "CLIPTextEncode", "mode": 0, "inputs": [{"name": "clip", "type": "CLIP", "link": 2}], "widgets_values": ["old pos"]},
                {"id": 4, "type": "EmptyLatentImage", "mode": 0, "inputs": [], "widgets_values": [832, 1216, 1]},
                {
                    "id": 5,
                    "type": "KSampler",
                    "mode": 0,
                    "inputs": [
                        {"name": "model", "type": "MODEL", "link": 3},
                        {"name": "positive", "type": "CONDITIONING", "link": 4},
                        {"name": "negative", "type": "CONDITIONING", "link": 5},
                        {"name": "latent_image", "type": "LATENT", "link": 6},
                    ],
                    "widgets_values": [123, "randomize", 28, 5.5, "er_sde", "simple", 1.0],
                },
                {"id": 6, "type": "VAEDecode", "mode": 0, "inputs": [{"name": "samples", "type": "LATENT", "link": 7}, {"name": "vae", "type": "VAE", "link": 8}]},
                {"id": 7, "type": "SaveImage", "mode": 0, "inputs": [{"name": "images", "type": "IMAGE", "link": 9}], "widgets_values": ["Test"]},
                {"id": 8, "type": "MarkdownNote", "mode": 0, "inputs": [], "widgets_values": ["notes"]},
            ],
            "links": [
                [1, 1, 1, 2, 0, "CLIP"],
                [2, 1, 1, 3, 0, "CLIP"],
                [3, 1, 0, 5, 0, "MODEL"],
                [4, 3, 0, 5, 1, "CONDITIONING"],
                [5, 2, 0, 5, 2, "CONDITIONING"],
                [6, 4, 0, 5, 3, "LATENT"],
                [7, 5, 0, 6, 0, "LATENT"],
                [8, 1, 2, 6, 1, "VAE"],
                [9, 6, 0, 7, 0, "IMAGE"],
            ],
        }
        api = comfyui.ui_to_api(ui, SAMPLE_OBJECT_INFO)
        self.assertNotIn("8", api)
        self.assertEqual(api["5"]["inputs"]["steps"], 28)
        self.assertEqual(api["5"]["inputs"]["cfg"], 5.5)
        self.assertEqual(api["5"]["inputs"]["sampler_name"], "er_sde")
        self.assertEqual(api["5"]["inputs"]["scheduler"], "simple")
        self.assertEqual(api["5"]["inputs"]["positive"], ["3", 0])
        self.assertEqual(api["5"]["inputs"]["negative"], ["2", 0])

        self.assertEqual(
            comfyui._workflow_sampler_defaults(api),
            {"steps": 28, "cfg": 5.5, "sampler": "er_sde", "scheduler": "simple", "denoise": 1.0},
        )

        rendered = comfyui.render(
            api,
            {
                "prompt": "new pos",
                "negative": "new neg",
                "seed": 999,
                "width": 1024,
                "height": 1024,
                "steps": 32,
                "cfg": 6.5,
                "sampler": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "ckpt": "",
            },
        )
        # Positive/negative resolved via sampler links even though node 2 < node 3
        self.assertEqual(rendered["3"]["inputs"]["text"], "new pos")
        self.assertEqual(rendered["2"]["inputs"]["text"], "new neg")
        # Empty ckpt preserves workflow's own checkpoint, while tweaked sampler knobs apply immediately
        self.assertEqual(rendered["1"]["inputs"]["ckpt_name"], "modelA.safetensors")
        self.assertEqual(rendered["5"]["inputs"]["steps"], 32)
        self.assertEqual(rendered["5"]["inputs"]["cfg"], 6.5)
        self.assertEqual(rendered["5"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(rendered["5"]["inputs"]["scheduler"], "normal")
        # Seed, size and prefix are still injected
        self.assertEqual(rendered["5"]["inputs"]["seed"], 999)
        self.assertEqual(rendered["4"]["inputs"]["width"], 1024)
        self.assertEqual(rendered["7"]["inputs"]["filename_prefix"], "pebble")

    def test_unet_override_and_loader_type(self):
        api_unet = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "orig.safetensors", "weight_dtype": "default"}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
            "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "orig"}},
        }
        self.assertEqual(comfyui._workflow_loader_type(api_unet), "unet")
        kept = comfyui.render(api_unet, {"prompt": "hi", "unet": ""})
        self.assertEqual(kept["1"]["inputs"]["unet_name"], "orig.safetensors")
        overridden = comfyui.render(api_unet, {"prompt": "hi", "unet": "new_fp8.safetensors"})
        self.assertEqual(overridden["1"]["inputs"]["unet_name"], "new_fp8.safetensors")

    def test_qwen21_dual_prompt_bypass_and_autogrow(self):
        ui = {
            "nodes": [
                {"id": 1, "type": "UNETLoader", "mode": 0, "inputs": [], "widgets_values_named": {"unet_name": "unetA.safetensors", "weight_dtype": "default"}},
                {"id": 4, "type": "TextEncodeQwenImage21", "mode": 0, "inputs": [
                    {"name": "images.image_1", "type": "IMAGE", "link": 10},
                    {"name": "images.image_2", "type": "IMAGE", "link": 11},
                ], "widgets_values_named": {"prompt": "old", "negative_prompt": "old_neg", "resolution": 1024}},
                {"id": 8, "type": "LoadImage", "mode": 0, "inputs": [], "widgets_values": ["ref.png", "image"]},
                {"id": 9, "type": "LoadImage", "mode": 4, "inputs": [], "widgets_values": ["ignored.png", "image"]},
                {"id": 5, "type": "KSampler", "mode": 0, "inputs": [
                    {"name": "model", "type": "MODEL", "link": 1},
                    {"name": "positive", "type": "CONDITIONING", "link": 2},
                    {"name": "negative", "type": "CONDITIONING", "link": 3},
                ], "widgets_values_named": {"seed": 1, "control_after_generate": "randomize", "steps": 25, "cfg": 3, "sampler_name": "euler", "scheduler": "simple", "denoise": 1}},
                {"id": 7, "type": "SaveImage", "mode": 0, "inputs": [{"name": "images", "type": "IMAGE", "link": 4}], "widgets_values_named": {"filename_prefix": "orig"}},
            ],
            "links": [
                [1, 1, 0, 5, 0, "MODEL"],
                [2, 4, 0, 5, 1, "CONDITIONING"],
                [3, 4, 1, 5, 2, "CONDITIONING"],
                [4, 5, 0, 7, 0, "IMAGE"],
                [10, 8, 0, 4, 1, "IMAGE"],
                [11, 9, 0, 4, 2, "IMAGE"],
            ],
        }
        api = comfyui.ui_to_api(ui, SAMPLE_OBJECT_INFO)
        self.assertNotIn("9", api)
        self.assertEqual(api["4"]["inputs"]["images.image_1"], ["8", 0])
        self.assertNotIn("images.image_2", api["4"]["inputs"])
        self.assertNotIn("control_after_generate", api["5"]["inputs"])

        rendered = comfyui.render(
            api,
            {
                "prompt": "anime rooftop",
                "negative": "bad hands",
                "seed": 42,
                "width": 1024,
                "height": 1024,
                "keep_workflow_sampler": True,
            },
        )
        self.assertEqual(rendered["4"]["inputs"]["prompt"], "anime rooftop")
        self.assertEqual(rendered["4"]["inputs"]["negative_prompt"], "bad hands")
        self.assertEqual(rendered["5"]["inputs"]["cfg"], 3)
        self.assertEqual(rendered["5"]["inputs"]["seed"], 42)

    def test_probe_discovers_image_workflows_only(self):
        img_ui = {
            "nodes": [
                {"id": 1, "type": "CheckpointLoaderSimple", "mode": 0, "inputs": [], "widgets_values": ["modelA.safetensors"]},
                {"id": 2, "type": "SaveImage", "mode": 0, "inputs": [], "widgets_values": ["img"]},
            ],
            "links": [],
        }
        video_ui = {
            "nodes": [
                {"id": 1, "type": "SaveVideo", "mode": 0, "inputs": [], "widgets_values": []},
            ],
            "links": [],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            raw_path = request.url.raw_path.decode("ascii").split("?", 1)[0]
            if raw_path == "/system_stats":
                return httpx.Response(200, json={"system": {"comfyui_version": "0.18.2"}, "devices": []})
            if raw_path == "/object_info":
                return httpx.Response(200, json=SAMPLE_OBJECT_INFO)
            if raw_path == "/userdata":
                return httpx.Response(200, json=["my_img.json", "my_video.json"])
            if raw_path == "/userdata/workflows%2Fmy_img.json":
                return httpx.Response(200, json=img_ui)
            if raw_path == "/userdata/workflows%2Fmy_video.json":
                return httpx.Response(200, json=video_ui)
            return httpx.Response(404)

        async def run():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                probed = await comfyui.probe("http://127.0.0.1:8188", "", client)
                loaded = await comfyui.load_user_workflow("http://127.0.0.1:8188", "", "comfyui:my_img", client)
                return probed, loaded

        probed, loaded = asyncio.run(run())
        self.assertTrue(probed["connected"])
        self.assertEqual(probed["models"]["unets"], ["unetA.safetensors"])
        self.assertEqual([w["id"] for w in probed["userWorkflows"]], ["comfyui:my_img"])
        self.assertEqual(probed["userWorkflows"][0]["loaderType"], "checkpoint")
        self.assertTrue(probed["userWorkflows"][0]["readonly"])
        self.assertEqual(loaded["1"]["inputs"]["ckpt_name"], "modelA.safetensors")

    def test_comfyui_prefix_guards(self):
        with self.assertRaises(comfyui.ComfyUIError):
            comfyui.delete_workflow("comfyui:ABCWUTU")
        with self.assertRaises(comfyui.ComfyUIError):
            comfyui._safe_comfy_rel_path("comfyui:../secret")


if __name__ == "__main__":
    unittest.main()
