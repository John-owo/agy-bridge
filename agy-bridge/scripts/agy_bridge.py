#!/usr/bin/env python3
"""Explicit, disposable workspace bridge to the local agy CLI.

This is context isolation, not an operating-system security sandbox. The child
process still runs with the caller's OS permissions. Never stage secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

RUNS_ROOT = Path.home() / "Documents" / "Codex" / "agy-bridge-runs"
MAX_FILES = 5000
MAX_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PROMPT_CHARS = 12000  # Leave room for Windows command-line quoting expansion.
EXCLUDED_DIRS = {".git", ".claude", ".codex", ".agents", "node_modules", "venv", ".venv", "dist", "build", "cache", ".cache", "__pycache__"}
EXCLUDED_FILES = {"agents.md", "claude.md", "skill.md", ".env", "id_rsa", "id_ed25519", "credentials", "credentials.json", "secrets.json", "settings.json"}
SECRET_PARTS = ("credential", "secret", "private", "token", "apikey", "api_key", "service-account", "service_account", ".pem", ".p12", ".pfx", ".key")
FORBIDDEN_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL", "AGY_ADC_AUTH", "GOOGLE_APPLICATION_CREDENTIALS")
TIMEOUT_RE = re.compile(r"timed?\s*out|timeout|deadline exceeded", re.I)
DENIAL_RE = re.compile(r"permission denied|access denied|action denied|tool (?:call )?denied|not allowed|a tool required the .* permission|no output produced|soft.denied", re.I)


class BridgeError(Exception):
    pass


def emit(data: dict, code: int = 0) -> int:
    print(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return code


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def reparse(path: Path) -> bool:
    stat = path.lstat()
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


def excluded(path: Path, is_dir: bool) -> bool:
    name = path.name.lower()
    if is_dir and name in EXCLUDED_DIRS:
        return True
    if not is_dir and (name in EXCLUDED_FILES or name.startswith(".env.")):
        return True
    return any(part in name for part in SECRET_PARTS)


def safe_relpath(value: str) -> Path:
    # PureWindowsPath catches drive/UNC forms even when tested on POSIX.
    from pathlib import PureWindowsPath
    win = PureWindowsPath(value)
    path = Path(value)
    if win.is_absolute() or win.drive or path.is_absolute() or any(p in ("..", "") for p in win.parts):
        raise BridgeError(f"Unsafe include path: {value}")
    if value in ("", "."):
        raise BridgeError("Include must name selected files or directories; full-source staging is disabled")
    return path


def resolve_agy() -> str:
    found = shutil.which("agy")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    candidate = Path(local) / "agy" / "bin" / "agy.exe" if local else Path("\0")
    if local and candidate.is_file():
        return str(candidate)
    raise BridgeError("agy executable not found on PATH or under LOCALAPPDATA/agy/bin")


def billing_guard() -> dict:
    active = [name for name in FORBIDDEN_ENV if os.environ.get(name)]
    if active:
        raise BridgeError("API/ADC billing environment variables are active: " + ", ".join(active))
    settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    if not settings.is_file():
        raise BridgeError(f"Local agy settings absent: {settings}")
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"Cannot parse local agy settings: {exc}") from exc
    if not isinstance(data, dict):
        raise BridgeError("Local agy settings must be a JSON object")
    if "useG1Credits" in data and data["useG1Credits"] is not False:
        raise BridgeError("Local agy useG1Credits must be false or omitted")
    # agy serializes this Go boolean with `omitempty`, so a saved false vanishes.
    # A shared Gemini config can supply inherited user settings; reject any
    # explicit true there before treating the omitted local value as false.
    shared_path = Path.home() / ".gemini" / "config" / "config.json"
    if shared_path.exists():
        try:
            shared = json.loads(shared_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError(f"Cannot parse shared Gemini config: {exc}") from exc
        if not isinstance(shared, dict):
            raise BridgeError("Shared Gemini config must be a JSON object")
        def credit_values(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "useG1Credits":
                        yield child
                    yield from credit_values(child)
            elif isinstance(value, list):
                for child in value:
                    yield from credit_values(child)
        for value in credit_values(shared):
            if value is not False:
                raise BridgeError("Shared Gemini config enables or ambiguously sets useG1Credits")
    provider = data.get("modelProvider")
    if provider not in (None, "", "default"):
        raise BridgeError("Custom/API modelProvider is not allowed")
    return {"settings_path": str(settings), "shared_config_path": str(shared_path), "useG1Credits": False, "credit_setting_source": "explicit-local-false" if "useG1Credits" in data else "omitted-default-false", "modelProvider": provider or "default", "api_environment_clear": True}


def run_capture(args: list[str], cwd: Path | None = None, timeout: int = 40) -> subprocess.CompletedProcess:
    try:
        process = subprocess.Popen(args, cwd=cwd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0, start_new_session=os.name != "nt")
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_tree(process)
            raise
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"agy diagnostic command timed out: {args[1:3]}") from exc


def parse_models(raw: str) -> list[dict]:
    models = []
    for line in raw.splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            models.append({"id": parts[0].strip(), "name": parts[1].strip()})
    return models


def extract_json(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # agy can wrap the slash-command response in an output-format JSON object.
        for line in reversed(raw.splitlines()):
            try:
                obj = json.loads(line)
                if isinstance(obj, (dict, list)):
                    return obj
            except json.JSONDecodeError:
                pass
    raise BridgeError("/usage did not return parseable JSON")


def usage_groups(obj) -> list[dict]:
    if isinstance(obj, dict) and "command" in obj:
        obj = obj["command"]
    if isinstance(obj, dict) and "data" in obj:
        obj = obj["data"]
    if isinstance(obj, str):
        obj = extract_json(obj)
        return usage_groups(obj)
    groups = obj.get("groups") if isinstance(obj, dict) else None
    if not isinstance(groups, list):
        raise BridgeError("/usage JSON has no command.data.groups list")
    return groups


def group_family(group: dict) -> str | None:
    desc = str(group.get("description", "")) + " " + str(group.get("name", ""))
    val = desc.lower()
    if "argon" in val:
        return "argon"
    if "gemini" in val and ("flash" in val or "pro" in val):
        return "flash"
    if "claude" in val or "gpt-oss" in val:
        return "claude"
    return None


def group_usable(group: dict) -> bool:
    buckets = group.get("buckets")
    if not isinstance(buckets, list) or not buckets:
        return False
    return all(isinstance(b, dict) and isinstance(b.get("remaining_fraction"), (int, float)) and not isinstance(b.get("remaining_fraction"), bool) and b["remaining_fraction"] > 0 for b in buckets)


def model_family(model: dict) -> str | None:
    value = (model["id"] + " " + model["name"]).lower()
    if "argon" in value:
        return "argon"
    if "flash" in value:
        return "flash"
    if "opus" in value or "sonnet" in value or "gpt-oss" in value:
        return "claude"
    return None


def choose_model(models: list[dict], groups: list[dict], tier: str, explicit: str | None = None) -> dict:
    by_family = {}
    for group in groups:
        family = group_family(group)
        if family and group_usable(group):
            by_family.setdefault(family, group)
    if explicit:
        candidates = [m for m in models if m["id"] == explicit]
        if not candidates:
            raise BridgeError("Explicit model ID is absent from agy model listing")
        model = candidates[0]
        family = model_family(model)
        if family not in by_family:
            raise BridgeError("Explicit model has no known group with usable quota")
        return {"model": model, "group": by_family[family]}
    preferences = {
        "routine": (("flash", "medium"), ("flash", "low"), ("argon", "low")),
        "standard": (("flash", "high"), ("argon", "medium")),
        "complex": (("argon", "high"), ("opus", "high"), ("sonnet", "high")),
    }[tier]
    for kind, effort in preferences:
        for model in models:
            value = (model["id"] + " " + model["name"]).lower()
            family = model_family(model)
            matched = kind in value and effort in value
            if matched and family in by_family:
                return {"model": model, "group": by_family[family]}
    raise BridgeError("No listed model matches the tier preference with a known usable quota group")


def diagnostics(agy: str) -> dict:
    # These commands are read-only. Raw output is retained to expose CLI version differences.
    models_result = run_capture([agy, "models"])
    usage_result = run_capture([agy, "-p", "/usage", "--output-format", "json", "--print-timeout", "40s"])
    result = {
        "models_command": {"exit_code": models_result.returncode, "stdout": models_result.stdout, "stderr": models_result.stderr},
        "usage_command": {"exit_code": usage_result.returncode, "stdout": usage_result.stdout, "stderr": usage_result.stderr},
        "models": parse_models(models_result.stdout),
    }
    result["ok"] = models_result.returncode == 0 and usage_result.returncode == 0
    try:
        result["usage"] = extract_json(usage_result.stdout)
        result["groups"] = usage_groups(result["usage"])
    except BridgeError as exc:
        result["usage_error"] = str(exc)
        result["groups"] = []
        result["ok"] = False
    if not result["models"]:
        result["ok"] = False
    return result


def prepare(source: Path, includes: list[str], owner: str, runs_root: Path = RUNS_ROOT) -> dict:
    if not source.exists() or reparse(source):
        raise BridgeError("Source must be an existing ordinary directory, not a link or reparse point")
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise BridgeError("Source must be a directory")
    if not includes:
        raise BridgeError("At least one --include is required")
    selected: dict[str, Path] = {}
    for item in includes:
        rel = safe_relpath(item)
        candidate = source / rel
        # Validate every component before resolving: links and reparse points are excluded.
        current = source
        for part in rel.parts:
            current = current / part
            if not current.exists() or reparse(current) or excluded(current, current.is_dir()):
                raise BridgeError(f"Included path is absent, linked, or excluded: {item}")
        if candidate.is_file():
            selected[candidate.relative_to(source).as_posix()] = candidate
        elif candidate.is_dir():
            for root, dirs, files in os.walk(candidate, followlinks=False):
                base = Path(root)
                dirs[:] = [d for d in dirs if not reparse(base / d) and not excluded(base / d, True)]
                for name in files:
                    path = base / name
                    if not reparse(path) and path.is_file() and not excluded(path, False):
                        selected[path.relative_to(source).as_posix()] = path
    if not selected:
        raise BridgeError("No eligible files selected")
    if len(selected) > MAX_FILES:
        raise BridgeError("Selected files exceed 5000-file limit")
    total = 0
    for path in selected.values():
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise BridgeError(f"Selected file exceeds 5 MiB limit: {path.name}")
        total += size
    if total > MAX_BYTES:
        raise BridgeError("Selected files exceed 100 MiB limit")
    runs_root.mkdir(parents=True, exist_ok=True)
    run_dir = runs_root / str(uuid.uuid4())
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    manifest = {"schema": 1, "owner": owner, "source": str(source), "context_isolation_only": True, "files": {}}
    try:
        for rel, origin in sorted(selected.items()):
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            # Recheck just before opening; do not follow a swapped link.
            if reparse(origin) or not origin.is_file():
                raise BridgeError(f"Source changed during staging: {rel}")
            source_hash = hash_file(origin)
            shutil.copyfile(origin, target, follow_symlinks=False)
            copied_hash = hash_file(target)
            if source_hash != copied_hash:
                raise BridgeError(f"Source changed during staging: {rel}")
            manifest["files"][rel] = {"source_sha256": source_hash, "copied_sha256": copied_hash, "bytes": target.stat().st_size}
        write_json(run_dir / "manifest.json", manifest)
        write_json(run_dir / "status.json", {"state": "PREPARED", "owner": owner})
    except Exception:
        shutil.rmtree(run_dir)
        raise
    return {"run_dir": str(run_dir), "workspace": str(workspace), "file_count": len(selected), "bytes": total, "owner": owner}


def validate_run(run_dir: Path) -> tuple[Path, dict]:
    run_dir = run_dir.resolve(strict=True)
    manifest_path = run_dir / "manifest.json"
    workspace = run_dir / "workspace"
    if not manifest_path.is_file() or not workspace.is_dir():
        raise BridgeError("Run directory is missing manifest or workspace")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        raise BridgeError("Invalid run manifest")
    return workspace, manifest


def terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10, shell=False)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def snapshot(workspace: Path) -> dict[str, str]:
    hashes = {}
    for root, dirs, files in os.walk(workspace, followlinks=False):
        base = Path(root)
        dirs[:] = [d for d in dirs if not reparse(base / d)]
        for name in files:
            path = base / name
            if path.is_file() and not reparse(path):
                hashes[path.relative_to(workspace).as_posix()] = hash_file(path)
    return hashes


def inspect(run_dir: Path) -> dict:
    workspace, manifest = validate_run(run_dir)
    baseline = {name: info["copied_sha256"] for name, info in manifest["files"].items()}
    current = snapshot(workspace)
    changes = {
        "created": {k: v for k, v in current.items() if k not in baseline},
        "modified": {k: v for k, v in current.items() if k in baseline and v != baseline[k]},
        "deleted": {k: baseline[k] for k in baseline if k not in current},
    }
    status_path = run_dir / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {"state": "UNKNOWN"}
    return {"run_dir": str(run_dir.resolve()), "workspace": str(workspace), "status": status, "changes": changes, "source_hashes": {k: v["source_sha256"] for k, v in manifest["files"].items()}}


def parse_child_result(raw: str, stderr: str, returncode: int, timed_out: bool) -> tuple[str, dict | None, str]:
    if timed_out or TIMEOUT_RE.search(stderr):
        return "FAILED_TIMEOUT", None, "Watchdog or CLI timeout"
    try:
        obj = extract_json(raw)
    except BridgeError:
        return "FAILED_OUTPUT", None, "Child output is not JSON"
    if not isinstance(obj, dict):
        return "FAILED_OUTPUT", None, "Child JSON is not an object"
    if returncode:
        return "FAILED_PROCESS", obj, f"agy exited {returncode}"
    if obj.get("status") != "SUCCESS":
        return "FAILED_CHILD", obj, "Child status is not SUCCESS"
    def has_denial(value):
        if isinstance(value, dict):
            return bool(value.get("denied_actions")) or any(has_denial(v) for v in value.values())
        if isinstance(value, list):
            return any(has_denial(v) for v in value)
        return False
    if has_denial(obj):
        return "FAILED_DENIAL", obj, "Child reported denied actions"
    if DENIAL_RE.search(stderr):
        return "FAILED_DENIAL", obj, "Child stderr indicates denial"
    response = obj.get("response")
    if not isinstance(response, str) or not response.strip():
        return "FAILED_OUTPUT", obj, "Child response is empty"
    return "SUCCESS", obj, ""


def run_child(run_dir: Path, task_file: Path, tier: str, model_id: str | None, timeout: int, agy: str | None = None, info: dict | None = None) -> dict:
    workspace, manifest = validate_run(run_dir)
    lock = run_dir / ".run.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise BridgeError("This run is already locked") from exc
    os.close(fd)
    try:
        status_path = run_dir / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("state") != "PREPARED":
            raise BridgeError("Run has already been used; prepare a fresh run")
        try:
            billing = billing_guard()
            agy = agy or resolve_agy()
            info = info or diagnostics(agy)
            if not info.get("ok", True):
                write_json(run_dir / "diagnostics.json", info)
                raise BridgeError("agy model or /usage diagnostics failed")
            picked = choose_model(info["models"], info["groups"], tier, model_id)
            task_file = task_file.resolve(strict=True)
            if workspace == task_file or workspace in task_file.parents:
                raise BridgeError("Task file must be outside the staged workspace")
            task = task_file.read_text(encoding="utf-8")
            prompt = ("You are an isolated implementation worker. Work only inside the current workspace. "
                      "Do not call agy or spawn another agent. Do not access credentials, external networks, "
                      "the original source, or paths outside this workspace. Report all changes and uncertainty.\n\n" + task)
            if not task.strip() or len(prompt) > MAX_PROMPT_CHARS:
                raise BridgeError("Task prompt must contain text and fit within 12000 characters including the wrapper")
            (run_dir / "task-input.txt").write_text(task, encoding="utf-8")
        except (BridgeError, OSError, ValueError, KeyError) as exc:
            failure = {"state": "FAILED_PREFLIGHT", "reason": str(exc), "selected": None, "response": None, "changes": inspect(run_dir)["changes"]}
            write_json(run_dir / "result.json", failure)
            write_json(status_path, {"state": "FAILED_PREFLIGHT", "reason": str(exc), "finished_at": time.time()})
            raise
        selected = {"id": picked["model"]["id"], "name": picked["model"]["name"], "group": picked["group"].get("name"), "group_description": picked["group"].get("description"), "tier": tier}
        write_json(run_dir / "selection.json", {"selected": selected, "billing": billing})
        write_json(status_path, {"state": "RUNNING", "selected": selected, "started_at": time.time()})
        args = [agy, "-p", prompt, "--output-format", "json", "--model", selected["id"], "--print-timeout", f"{timeout}s"]
        stdout_path, stderr_path = run_dir / "stdout.txt", run_dir / "stderr.txt"
        started = time.time()
        timed_out = False
        try:
            with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
                process = subprocess.Popen(args, cwd=workspace, stdout=out, stderr=err, shell=False, text=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0, start_new_session=os.name != "nt")
                try:
                    process.wait(timeout=timeout + 20)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_tree(process)
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            state, obj, reason = parse_child_result(stdout, stderr, process.returncode, timed_out)
        except Exception as exc:
            state, obj, reason = "FAILED_PROCESS", None, str(exc)
            stderr_path.write_text(reason, encoding="utf-8")
            stdout_path.touch(exist_ok=True)
        result = {"state": state, "reason": reason, "selected": selected, "exit_code": process.returncode if 'process' in locals() else None, "elapsed_seconds": round(time.time() - started, 2), "response": obj.get("response") if isinstance(obj, dict) else None, "raw_result": obj, "changes": inspect(run_dir)["changes"]}
        write_json(run_dir / "result.json", result)
        write_json(status_path, {"state": state, "reason": reason, "selected": selected, "finished_at": time.time()})
        return {"run_dir": str(run_dir), "result_path": str(run_dir / "result.json"), "stdout_path": str(stdout_path), "stderr_path": str(stderr_path), "status": state, "reason": reason, "selected": selected}
    finally:
        lock.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    p = sub.add_parser("prepare")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--include", action="append", default=[])
    p.add_argument("--owner", choices=("codex", "cc"), required=True)
    p = sub.add_parser("run")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--task-file", type=Path, required=True)
    p.add_argument("--tier", choices=("routine", "standard", "complex"), required=True)
    p.add_argument("--model")
    p.add_argument("--timeout", type=int, default=180)
    p = sub.add_parser("inspect")
    p.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            billing = billing_guard()
            agy = resolve_agy()
            info = diagnostics(agy)
            return emit({"agy": agy, "billing": billing, **info}, 0 if info["ok"] else 1)
        if args.command == "prepare":
            return emit({"ok": True, **prepare(args.source, args.include, args.owner)})
        if args.command == "inspect":
            return emit({"ok": True, **inspect(args.run_dir)})
        if not 1 <= args.timeout <= 3600:
            raise BridgeError("--timeout must be 1–3600 seconds")
        result = run_child(args.run_dir, args.task_file, args.tier, args.model, args.timeout)
        return emit({"ok": result["status"] == "SUCCESS", **result}, 0 if result["status"] == "SUCCESS" else 1)
    except (BridgeError, OSError, ValueError, KeyError) as exc:
        return emit({"ok": False, "error": str(exc)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
