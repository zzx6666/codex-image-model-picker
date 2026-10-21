from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "codex_image_model_picker",
    ROOT / "codex_image_model_picker.py",
)
assert SPEC and SPEC.loader
image2 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = image2
SPEC.loader.exec_module(image2)

PNG = b"\x89PNG\r\n\x1a\n"
PNG_B64 = base64.b64encode(PNG).decode("ascii")
JPEG = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg"


class Provider:
    def __init__(self, models: list[str] | None = None, fail_images: bool = False):
        self.models = models or ["chat-model", "gpt-image-2.5-sunburst"]
        self.fail_images = fail_images
        self.probes = 0
        self.images: list[dict[str, object]] = []
        self.requests: list[tuple[str, str, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                outer.probes += 1
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                body = json.dumps({"data": [{"id": model} for model in outer.models]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                outer.requests.append((self.path, self.headers.get("Authorization", ""), body))
                if self.path == "/v1/images/generations":
                    value = json.loads(body)
                    model = value.get("model")
                elif self.path == "/v1/images/edits":
                    marker = b'name="model"\r\n\r\n'
                    start = body.find(marker)
                    end = body.find(b"\r\n", start + len(marker))
                    model = body[start + len(marker) : end].decode() if start >= 0 else ""
                else:
                    self.send_error(404)
                    return
                if outer.fail_images:
                    response = b'{"error":{"message":"synthetic failure"}}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    return
                outer.images.append({"path": self.path, "model": model})
                response = json.dumps({"data": [{"b64_json": PNG_B64}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class CodexImageModelPickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.old_config = os.environ.get("CODEX_IMAGE2_CONFIG")
        os.environ["CODEX_IMAGE2_CONFIG"] = str(self.config)
        self.key = "synthetic-model-list-key"
        image2.ConfigStore(self.config).save("https://example.com", self.key)

    def tearDown(self):
        if self.old_config is None:
            os.environ.pop("CODEX_IMAGE2_CONFIG", None)
        else:
            os.environ["CODEX_IMAGE2_CONFIG"] = self.old_config
        self.temp.cleanup()

    def test_models_output_is_sorted_and_redacted(self):
        provider = Provider(["chat-z", "gpt-image-2", "gpt-image-2.5", self.key, "chat-a"])
        try:
            image2.ConfigStore(self.config).save(provider.url, self.key)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                image2.run_models([])
            value = json.loads(output.getvalue())
            self.assertEqual(
                value["image_models"],
                ["gpt-image-2.5", "gpt-image-2"],
            )
            self.assertTrue(value["selection_required"])
            self.assertNotIn(self.key, output.getvalue())
            self.assertEqual(provider.probes, 1)
            self.assertEqual(provider.images, [])
        finally:
            provider.close()

    def test_environment_key_without_url_is_rejected(self):
        with mock.patch.dict(
            os.environ,
            {"CODEX_API_KEY": self.key, "CODEX_API_URL": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(image2.AppError, "CODEX_API_URL"):
                image2.ConfigStore(self.root / "missing.json").resolve(require_key=True)

    def test_legacy_config_is_migrated_to_new_location(self):
        legacy = self.root / "legacy" / "config.json"
        new = self.root / "new" / "config.json"
        image2.ConfigStore(legacy).save("https://example.com", self.key)
        store = image2.ConfigStore(new)
        store.legacy_path = legacy
        settings = store.resolve(require_key=True)
        self.assertEqual(settings.api_key, self.key)
        self.assertEqual(settings.api_url, "https://example.com/v1")
        self.assertTrue(new.is_file())
        self.assertEqual(
            json.loads(new.read_text(encoding="utf-8"))["api_url"],
            "https://example.com/v1",
        )

    def test_normalize_address_validates_port_and_loopback(self):
        self.assertEqual(
            image2.normalize_api_base("http://127.0.0.1:8080"),
            "http://127.0.0.1:8080/v1",
        )
        self.assertEqual(
            image2.normalize_api_base("https://example.com:443"),
            "https://example.com:443/v1",
        )
        with self.assertRaisesRegex(image2.AppError, "端口无效"):
            image2.normalize_api_base("https://example.com:bad")
        with self.assertRaisesRegex(image2.AppError, "https"):
            image2.normalize_api_base("http://example.com")
        with self.assertRaisesRegex(image2.AppError, "账号、密码"):
            image2.normalize_api_base("https://user:pass@example.com")

    def test_invalid_image_payload_and_extension_alignment(self):
        invalid = base64.b64encode(b"<html>not an image</html>").decode()
        with self.assertRaisesRegex(image2.AppError, "不是支持的"):
            image2.decode_images(
                json.dumps({"data": [{"b64_json": invalid}]}).encode(),
                1,
            )
        aligned = image2.align_image_extensions(
            [self.root / "1. 测试.png"],
            [JPEG],
        )
        self.assertEqual(aligned, [self.root / "1. 测试.jpg"])
        with self.assertRaisesRegex(image2.AppError, "无效的图片链接"):
            image2.download_image_url("http://example.com/image.png", 1)
        with self.assertRaisesRegex(image2.AppError, "无效的图片链接"):
            image2.download_image_url("https://user:pass@example.com/image.png", 1)

    def test_default_name_and_sequence_continue_existing_numbering(self):
        work = self.root / "work"
        work.mkdir()
        (work / "1. 旧图片.png").write_bytes(PNG)
        paths = image2.output_paths(
            "",
            str(work),
            "一段很长的提示词",
            1,
            "花村：樱花/鸟居与城堡",
        )
        self.assertEqual(paths, [work / "2. 花村：樱花-鸟居与城堡.png"])

        numbered = image2.output_paths("", str(work), "提示词", 3, "花村樱花鸟居")
        self.assertEqual(
            numbered,
            [
                work / "2. 花村樱花鸟居.png",
                work / "3. 花村樱花鸟居.png",
                work / "4. 花村樱花鸟居.png",
            ],
        )

    def test_generate_saves_to_working_directory_with_automatic_name(self):
        provider = Provider()
        work = self.root / "current"
        work.mkdir()
        (work / "1. 旧图片.png").write_bytes(PNG)
        old_cwd = Path.cwd()
        try:
            image2.ConfigStore(self.config).save(provider.url, self.key)
            os.chdir(work)
            args = image2.parse_generate(
                [
                    "--model",
                    "gpt-image-2.5-sunburst",
                    "--name",
                    "花村樱花鸟居与日式城堡",
                ]
            )
            result = image2.generate(
                "花村场景", args.out, args, image2.ModelSelector()
            )
            expected = work / "2. 花村樱花鸟居与日式城堡.png"
            self.assertEqual(Path(result["outputs"][0]), expected.resolve())
            self.assertEqual(expected.read_bytes(), PNG)
            self.assertFalse((work / "output").exists())
        finally:
            os.chdir(old_cwd)
            provider.close()

    def test_sunburst_reaches_generation_edit_and_batch(self):
        provider = Provider()
        try:
            image2.ConfigStore(self.config).save(provider.url, self.key)
            out_dir = self.root / "output"
            args = image2.parse_generate(
                [
                    "--model",
                    "gpt-image-2.5-sunburst",
                    "--size",
                    "1024x1024",
                    "--quality",
                    "low",
                    "--out-dir",
                    str(out_dir),
                    "--out",
                    "generated.png",
                ]
            )
            result = image2.generate("generate prompt", args.out, args, image2.ModelSelector())
            self.assertEqual(result["model"], "gpt-image-2.5-sunburst")

            source = self.root / "input.png"
            source.write_bytes(PNG)
            edit_args = image2.parse_edit(
                [
                    "--model",
                    "gpt-image-2.5-sunburst",
                    "--image",
                    str(source),
                    "--quality",
                    "low",
                    "--out-dir",
                    str(out_dir),
                    "--out",
                    "edited.png",
                ]
            )
            result = image2.edit(
                "edit prompt",
                edit_args.image,
                edit_args.mask,
                edit_args.out,
                edit_args,
                image2.ModelSelector(),
            )
            self.assertEqual(result["model"], "gpt-image-2.5-sunburst")

            jobs = self.root / "jobs.jsonl"
            jobs.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "prompt": "one",
                                "model": "gpt-image-2.5-sunburst",
                                "out": "one.png",
                            }
                        ),
                        json.dumps(
                            {
                                "prompt": "two",
                                "model": "gpt-image-2.5-sunburst",
                                "out": "two.png",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                image2.run_batch(
                    [
                        "--input",
                        str(jobs),
                        "--out-dir",
                        str(out_dir),
                        "--concurrency",
                        "2",
                    ]
                )
            summary = json.loads(output.getvalue())
            self.assertEqual((summary["succeeded"], summary["failed"]), (2, 0))
            self.assertEqual(
                [entry["model"] for entry in provider.images],
                ["gpt-image-2.5-sunburst"] * 4,
            )
            self.assertEqual(provider.probes, 0)
            for name in ("generated.png", "edited.png", "one.png", "two.png"):
                self.assertEqual((out_dir / name).read_bytes(), PNG)
        finally:
            provider.close()

    def test_batch_default_names_follow_jsonl_order(self):
        provider = Provider()
        out_dir = self.root / "batch-default"
        out_dir.mkdir()
        (out_dir / "1. 已有图片.png").write_bytes(PNG)
        try:
            image2.ConfigStore(self.config).save(provider.url, self.key)
            jobs = self.root / "batch-default.jsonl"
            jobs.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "prompt": "樱花鸟居",
                                "name": "花村樱花鸟居",
                                "model": "gpt-image-2.5-sunburst",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "prompt": "日式城堡",
                                "name": "花村日式城堡",
                                "model": "gpt-image-2.5-sunburst",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                image2.run_batch(
                    [
                        "--input",
                        str(jobs),
                        "--out-dir",
                        str(out_dir),
                        "--concurrency",
                        "2",
                    ]
                )
            summary = json.loads(output.getvalue())
            self.assertEqual((summary["succeeded"], summary["failed"]), (2, 0))
            self.assertEqual(
                (out_dir / "2. 花村樱花鸟居.png").read_bytes(),
                PNG,
            )
            self.assertEqual(
                (out_dir / "3. 花村日式城堡.png").read_bytes(),
                PNG,
            )
        finally:
            provider.close()

    def test_batch_rejects_unknown_or_invalid_jobs_before_charging(self):
        provider = Provider()
        try:
            image2.ConfigStore(self.config).save(provider.url, self.key)
            unknown = self.root / "unknown.jsonl"
            unknown.write_text(
                '{"prompt":"测试","typo":"value"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(image2.AppError, "未知字段"):
                image2.run_batch(["--input", str(unknown)])

            invalid = self.root / "invalid.jsonl"
            invalid.write_text(
                '{"prompt":"测试","n":0}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(image2.AppError, "1 到 10"):
                image2.run_batch(["--input", str(invalid)])
            self.assertEqual(provider.requests, [])
        finally:
            provider.close()

    def test_batch_rejects_duplicate_output_paths_before_charging(self):
        provider = Provider()
        try:
            image2.ConfigStore(self.config).save(provider.url, self.key)
            jobs = self.root / "duplicates.jsonl"
            jobs.write_text(
                "\n".join(
                    [
                        '{"prompt":"一","out":"same.png"}',
                        '{"prompt":"二","out":"same.png"}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(image2.AppError, "重复输出路径"):
                image2.run_batch(["--input", str(jobs)])
            self.assertEqual(provider.requests, [])
        finally:
            provider.close()

    def test_batch_fail_fast_reports_skipped_jobs(self):
        provider = Provider(fail_images=True)
        try:
            image2.ConfigStore(self.config).save(provider.url, self.key)
            jobs = self.root / "fail-fast.jsonl"
            jobs.write_text(
                "\n".join(
                    [
                        '{"prompt":"失败","name":"失败任务","model":"gpt-image-2.5-sunburst"}',
                        '{"prompt":"跳过","name":"跳过任务","model":"gpt-image-2.5-sunburst"}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(image2.AppError, "批量任务失败"):
                    image2.run_batch(
                        [
                            "--input",
                            str(jobs),
                            "--out-dir",
                            str(self.root / "fail-fast-output"),
                            "--concurrency",
                            "1",
                            "--fail-fast",
                            "--max-attempts",
                            "1",
                        ]
                    )
            summary = json.loads(output.getvalue())
            self.assertEqual(
                (summary["succeeded"], summary["failed"], summary["skipped"]),
                (0, 1, 1),
            )
        finally:
            provider.close()

    def test_auto_mode_discovers_once_and_dry_run_does_not_contact_provider(self):
        provider = Provider(["gpt-image-2.5-sunburst"])
        try:
            image2.ConfigStore(self.config).save(provider.url, self.key)
            out_dir = self.root / "auto"
            args = image2.parse_generate(
                [
                    "--model",
                    "auto",
                    "--out-dir",
                    str(out_dir),
                    "--out",
                    "auto.png",
                ]
            )
            result = image2.generate("auto prompt", args.out, args, image2.ModelSelector())
            self.assertEqual(result["model"], "gpt-image-2.5-sunburst")
            self.assertEqual(provider.probes, 1)

            dry_args = image2.parse_generate(
                [
                    "--model",
                    "auto",
                    "--dry-run",
                    "--out",
                    "dry.png",
                ]
            )
            calls = len(provider.requests)
            result = image2.generate("dry prompt", dry_args.out, dry_args, image2.ModelSelector())
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["model_selection"], "auto")
            self.assertEqual(len(provider.requests), calls)
        finally:
            provider.close()

    def test_connection_finds_sunburst_without_paid_request(self):
        provider = Provider(["chat-only", "gpt-image-2.5-sunburst"])
        try:
            result = image2.check_connection(provider.url + "/v1", self.key)
            self.assertTrue(result["ok"])
            self.assertEqual(result["model"], "gpt-image-2.5-sunburst")
            self.assertIn("gpt-image-2.5-sunburst", result["models"])
            self.assertEqual(provider.images, [])
        finally:
            provider.close()

    def test_generation_follows_image_download_redirect(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                if self.path == "/v1/models":
                    body = json.dumps(
                        {"data": [{"id": "gpt-image-2.5-sunburst"}]}
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/image.png")
                    self.end_headers()
                elif self.path == "/image.png":
                    if not self.headers.get("User-Agent", "").startswith("Mozilla/5.0"):
                        self.send_error(403)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(PNG)))
                    self.end_headers()
                    self.wfile.write(PNG)
                else:
                    self.send_error(404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                response = json.dumps(
                    {
                        "data": [
                            {
                                "url": f"http://127.0.0.1:{self.server.server_port}/redirect"
                            }
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        provider_url = f"http://127.0.0.1:{server.server_port}"
        try:
            image2.ConfigStore(self.config).save(provider_url, self.key)
            args = image2.parse_generate(
                [
                    "--model",
                    "gpt-image-2.5-sunburst",
                    "--out-dir",
                    str(self.root / "redirect-output"),
                    "--out",
                    "redirected.png",
                ]
            )
            result = image2.generate(
                "redirect test", args.out, args, image2.ModelSelector()
            )
            self.assertEqual(result["model"], "gpt-image-2.5-sunburst")
            self.assertEqual(
                Path(result["outputs"][0]).read_bytes(),
                PNG,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_cross_origin_image_redirect_does_not_leak_authorization(self):
        leaked = []

        class Destination(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                leaked.append(self.headers.get("Authorization", ""))
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(PNG)))
                self.end_headers()
                self.wfile.write(PNG)

        destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
        destination_thread = threading.Thread(
            target=destination.serve_forever,
            daemon=True,
        )
        destination_thread.start()
        destination_url = (
            f"http://127.0.0.1:{destination.server_port}/final.png"
        )

        class Origin(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                if self.path == "/v1/models":
                    body = b'{"data":[{"id":"gpt-image-2.5-sunburst"}]}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", destination_url)
                    self.end_headers()
                else:
                    self.send_error(404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = json.dumps(
                    {
                        "data": [
                            {
                                "url": f"http://127.0.0.1:{self.server.server_port}/redirect"
                            }
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()
        origin_url = f"http://127.0.0.1:{origin.server_port}"
        try:
            image2.ConfigStore(self.config).save(origin_url, self.key)
            args = image2.parse_generate(
                [
                    "--model",
                    "gpt-image-2.5-sunburst",
                    "--out-dir",
                    str(self.root / "cross-origin"),
                    "--out",
                    "image.png",
                ]
            )
            image2.generate("redirect", args.out, args, image2.ModelSelector())
            self.assertEqual(leaked, [""])
        finally:
            origin.shutdown()
            origin.server_close()
            origin_thread.join(2)
            destination.shutdown()
            destination.server_close()
            destination_thread.join(2)

    def test_configuration_server_requires_token_origin_and_never_exposes_key(self):
        store = image2.ConfigStore(self.config)
        server = image2.ConfigurationServer(("127.0.0.1", 0), store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = "http://" + server.host
        try:
            status = urllib.request.urlopen(
                urllib.request.Request(
                    origin + "/api/status",
                    headers={"Authorization": "Bearer " + server.token},
                ),
                timeout=3,
            )
            body = status.read().decode()
            self.assertIn('"configured": true', body)
            self.assertNotIn(self.key, body)

            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(
                    urllib.request.Request(
                        origin + "/api/status",
                        headers={"Authorization": "Bearer wrong"},
                    ),
                    timeout=3,
                )
            self.assertEqual(rejected.exception.code, 401)

            with self.assertRaises(urllib.error.HTTPError) as cross_origin:
                urllib.request.urlopen(
                    urllib.request.Request(
                        origin + "/api/close",
                        data=b"{}",
                        headers={
                            "Authorization": "Bearer " + server.token,
                            "Origin": "https://attacker.example",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    ),
                    timeout=3,
                )
            self.assertEqual(cross_origin.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_configure_launches_detached_page_that_can_close_itself(self):
        output = io.StringIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            with contextlib.redirect_stdout(output):
                image2.run_configure(["--no-browser"])
        launch = json.loads(output.getvalue())
        parsed = urllib.parse.urlsplit(launch["url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        token = parsed.fragment
        self.assertFalse(launch["browser_opened"])
        self.assertTrue(token)

        request = urllib.request.Request(
            origin + "/api/close",
            data=b"{}",
            headers={
                "Authorization": "Bearer " + token,
                "Origin": origin,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(origin + "/api/status", timeout=0.2)
            except Exception:
                break
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
