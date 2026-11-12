#!/usr/bin/env python3
"""Local 0.2.0 acceptance only: no AWS discovery, deployment or fulfillment."""
import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CHANGE = "add-hyd-b300-reliable-spot"


def blocked_aws_request(*args, **kwargs):
    raise AssertionError("release-check blocks ALL botocore HTTP requests, including custom AWS endpoints")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openspec", action="store_true", help="also strictly validate the approved OpenSpec change (CLI must be installed)")
    parser.add_argument("--quiet", action="store_true", help="print compact regression results")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)  # CDK assets and existing relative fixtures are project-root based.
    os.environ.update({
        "AWS_ACCESS_KEY_ID": "offline-release-check", "AWS_SECRET_ACCESS_KEY": "offline-release-check",
        "AWS_SESSION_TOKEN": "offline-release-check", "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_DEFAULT_REGION": "ap-south-2", "AWS_REGION": "ap-south-2",
        "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "PYTHONDONTWRITEBYTECODE": "1", "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
    })
    for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
                "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        os.environ.pop(key, None)
    from botocore.endpoint import Endpoint
    from botocore.httpsession import URLLib3Session
    # Patch SDK transport, not sockets: CDK jsii needs its local Node process.
    # Both hooks block before requests leave Python, even when tests customize endpoints.
    with ExitStack() as safety:
        safety.enter_context(patch.object(URLLib3Session, "send", blocked_aws_request))
        safety.enter_context(patch.object(Endpoint, "_send", blocked_aws_request))
        from h100_spot_controller import __version__
        from h100_spot_controller.config import load_target
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        if __version__ != "0.2.0" or metadata["project"]["version"] != __version__:
            parser.error("package and project versions must both be 0.2.0")
        examples = sorted((ROOT / "config").glob("*.example.yaml"))
        if not (ROOT / "config" / "hyd-b300.example.yaml").is_file():
            parser.error("HYD B300 disabled example is missing")
        for path in examples:
            target = load_target(path)
            if target.enabled:
                parser.error(f"example must remain disabled: {path.name}")
        print(f"Version {__version__}; validated {len(examples)} disabled examples offline", flush=True)
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
        result = unittest.TextTestRunner(verbosity=1 if args.quiet else 2).run(suite)
        print(f"OFFLINE tests={result.testsRun} failures={len(result.failures)} errors={len(result.errors)} skipped={len(result.skipped)}", flush=True)
        if not result.wasSuccessful() or result.skipped:
            return 1
    if args.openspec:
        executable = shutil.which("openspec")
        if executable is None:
            parser.error("--openspec requires the installed openspec CLI on PATH")
        checked = subprocess.run([executable, "validate", CHANGE, "--strict"], cwd=ROOT, check=False)
        if checked.returncode:
            return checked.returncode
    print("HYD/B300 AWS fulfillment, interruption/replenishment, deployment and account preflight: NOT RUN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
