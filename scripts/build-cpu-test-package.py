#!/usr/bin/env python3
"""Offline CPU package/template builder; never install or call an AWS service."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from h100_spot_controller.cpu_test_deployment import build_cpu_package, build_cpu_template


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="NEW Lambda zip; existing files are never replaced")
    parser.add_argument("--template-output", type=Path, help="NEW CloudFormation JSON; requires reviewed runtime policy")
    parser.add_argument("--runtime-policy", type=Path, help="Root-reviewed Autopilot-derived identity policy JSON")
    parser.add_argument("--run-id")
    parser.add_argument("--target-id")
    args = parser.parse_args()
    template = None
    if args.template_output:
        if not all((args.runtime_policy, args.run_id, args.target_id)):
            parser.error("template requires --runtime-policy, --run-id and --target-id (no default IAM)")
        if args.template_output.exists() or args.template_output.is_symlink():
            parser.error("template output already exists; use a new path")
        template = build_cpu_template(runtime_policy=json.loads(args.runtime_policy.read_text(encoding="utf-8")),
                                      run_id=args.run_id, target_id=args.target_id)
    elif args.runtime_policy or args.run_id or args.target_id:
        parser.error("template options require --template-output")
    created = []
    success = False
    try:
        manifest = build_cpu_package(ROOT, args.output)
        created.append(args.output.resolve(strict=True))
        if template is not None:
            with args.template_output.open("x", encoding="utf-8") as handle:
                created.append(args.template_output.resolve(strict=True))
                json.dump(template, handle, indent=2, sort_keys=True)
                handle.write("\n")
        print(json.dumps({"status": "built-offline", "package_sha256": manifest["zip_sha256"],
                          "runtime_file_count": len(manifest["files"]), "handler": manifest["handler"],
                          "iam_review": "root-required", "aws_operations": "NOT RUN"}, sort_keys=True))
        success = True
        return 0
    finally:
        if not success:
            for path in reversed(created):
                if path.exists():
                    assert path.resolve() == path and path.is_file() and not path.is_symlink()
                    path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
