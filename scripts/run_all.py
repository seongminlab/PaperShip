#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROJECTS_DIR = ROOT / "projects"
LOGS_DIR = ROOT / "logs"


def read_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if not path.exists():
        return data

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = parse_value(value.strip())
    return data


def parse_value(value: str) -> Any:
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return value.strip("\"'")


def setup_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    log_path = LOGS_DIR / f"daily-{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def iter_project_dirs() -> list[Path]:
    return sorted(
        path
        for path in PROJECTS_DIR.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    )


def load_task_module(task_path: Path):
    module_name = f"automation_{task_path.parent.name}"
    spec = importlib.util.spec_from_file_location(module_name, task_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {task_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_project(project_dir: Path) -> bool:
    config = read_simple_yaml(project_dir / "project.yaml")
    name = str(config.get("name") or project_dir.name)

    if config.get("enabled") is not True:
        logging.info("Skipped disabled project: %s", name)
        return True

    task_path = project_dir / "task.py"
    if not task_path.exists():
        logging.error("Missing task.py for project: %s", name)
        return False

    logging.info("Starting project: %s", name)
    module = load_task_module(task_path)
    if not hasattr(module, "run"):
        logging.error("Project has no run() function: %s", name)
        return False

    module.run()
    logging.info("Finished project: %s", name)
    return True


def main() -> int:
    setup_logging()
    logging.info("Daily automation started")

    failures = 0
    for project_dir in iter_project_dirs():
        try:
            if not run_project(project_dir):
                failures += 1
        except Exception:
            failures += 1
            logging.exception("Project failed: %s", project_dir.name)

    if failures:
        logging.error("Daily automation finished with %s failure(s)", failures)
        return 1

    logging.info("Daily automation finished successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
