from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = PROJECT_ROOT / "docs" / "rag_eval_web.html"
BENCHMARK_PATH = PROJECT_ROOT / "src" / "benchmark_zh_rag_vs_gpt.py"
QUESTION_RE = re.compile(r"^-\s+(.+?):\s*(.+?)\s*$")


def run_question(question: str, timeout: int, report_ttl_days: int) -> dict[str, Any]:
    question = " ".join(question.split()).strip()
    if not question:
        raise ValueError("Question is empty.")
    if len(question) > 1000:
        raise ValueError("Question is too long; keep it under 1000 characters.")

    cleanup_expired_reports(report_ttl_days)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory(prefix="rag-gpt-vs-rag-web-") as tmp:
        tmp_path = Path(tmp)
        questions_path = tmp_path / "questions.txt"
        output_path = PROJECT_ROOT / "data" / f"eval_zh_gpt_vs_rag_web_{timestamp}.md"
        questions_path.write_text(f"# Web Input\n\n1. {question}\n", encoding="utf-8")

        env = os.environ.copy()
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("HF_HUB_DISABLE_XET", "1")

        command = [
            str(PROJECT_ROOT / ".venv" / "bin" / "python"),
            str(BENCHMARK_PATH),
            "--questions",
            str(questions_path),
            "--output",
            str(output_path),
        ]
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "\n".join(
                    part
                    for part in [
                        f"benchmark_zh_rag_vs_gpt.py failed with exit code {completed.returncode}.",
                        completed.stdout.strip(),
                        completed.stderr.strip(),
                    ]
                    if part
                )
            )

        report = output_path.read_text(encoding="utf-8")
        cleanup_expired_reports(report_ttl_days)
        return {
            "report": report,
            "output_path": str(output_path.relative_to(PROJECT_ROOT)),
            "metrics": parse_metrics(report),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }


def parse_metrics(report: str) -> dict[str, str]:
    metrics: dict[str, str] = {}
    mapping = {
        "Retrieval strength": "retrieval_strength",
        "Confidence": "retrieval_strength",
        "Evidence diversity": "evidence_diversity",
        "Top1 score": "top1",
        "Top5 avg score": "top5",
        "Coverage": "coverage",
        "Evidence count": "evidence_count",
    }
    for line in report.splitlines():
        match = QUESTION_RE.match(line)
        if not match:
            continue
        key, value = match.groups()
        target = mapping.get(key)
        if target:
            metrics[target] = value
    return metrics


class Handler(BaseHTTPRequestHandler):
    server_version = "RAGEvalWeb/0.1"

    def do_GET(self) -> None:
        path = request_path(self.path)
        if path not in {"/", "/index.html"}:
            self.send_json({"ok": False, "error": "Not found."}, status=404)
            return
        body = HTML_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = request_path(self.path)
        if path != "/api/run":
            self.send_json({"ok": False, "error": "Not found."}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 20000:
                raise ValueError("Request body is too large.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = run_question(
                str(payload.get("question", "")),
                self.server.timeout_seconds,
                self.server.report_ttl_days,
            )
            self.send_json({"ok": True, **result})
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def request_path(raw_path: str) -> str:
    parsed = urlsplit(raw_path)
    if parsed.scheme and parsed.netloc:
        return parsed.path or "/"
    if raw_path in {"", "*"}:
        return "/"
    if raw_path.startswith("/"):
        return raw_path.split("?", 1)[0]
    return "/"


def cleanup_expired_reports(ttl_days: int) -> list[Path]:
    if ttl_days <= 0:
        return []
    cutoff = time.time() - ttl_days * 24 * 60 * 60
    removed: list[Path] = []
    for pattern in ("eval_zh_gpt_vs_rag_web_*.md", "eval_zh_web_*.md"):
        for path in (PROJECT_ROOT / "data").glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed.append(path)
            except FileNotFoundError:
                continue
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Local web UI for Chinese GPT-only vs RAG benchmark runs.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument(
        "--report-ttl-days",
        type=int,
        default=7,
        help="Delete web-generated markdown reports older than this many days. Set 0 to disable.",
    )
    args = parser.parse_args()

    if not HTML_PATH.exists():
        raise SystemExit(f"Missing HTML page: {HTML_PATH}")
    if not BENCHMARK_PATH.exists():
        raise SystemExit(f"Missing benchmark script: {BENCHMARK_PATH}")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.timeout_seconds = args.timeout  # type: ignore[attr-defined]
    server.report_ttl_days = args.report_ttl_days  # type: ignore[attr-defined]
    print(f"Serving GPT-only vs RAG eval UI at http://{args.host}:{args.port}", flush=True)
    print(f"Web report TTL: {args.report_ttl_days} days", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
