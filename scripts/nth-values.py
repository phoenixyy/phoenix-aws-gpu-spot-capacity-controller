#!/usr/bin/env python3
"""Print reviewable NTH Helm values; never contact AWS or Kubernetes."""
import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml
from h100_spot_controller.config import ConfigurationError, load_target
from h100_spot_controller.nth import CHART, CHART_VERSION, APP_VERSION, nth_values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--region", required=True, help="candidate Region whose EKS cluster will receive NTH")
    parser.add_argument("--kubernetes-version", required=True, help="reviewed cluster minor version, e.g. 1.32")
    parser.add_argument("--enable-draining", action="store_true")
    parser.add_argument("--acknowledge-cluster-rbac", action="store_true", help="acknowledge cluster-wide node patch/Pod eviction and workload disruption")
    args = parser.parse_args()
    try:
        values = nth_values(load_target(args.path), args.region, args.kubernetes_version,
                            enable_draining=args.enable_draining, acknowledge_cluster_rbac=args.acknowledge_cluster_rbac)
    except (ConfigurationError, ValueError, KeyError) as error:
        parser.error(str(error))
    print(f"# chart={CHART} version={CHART_VERSION} app={APP_VERSION}")
    print("# Scheduling selectors are NOT an RBAC security boundary.")
    print(yaml.safe_dump(values, sort_keys=False), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
