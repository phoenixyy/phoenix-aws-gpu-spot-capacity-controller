#!/usr/bin/env python3
import aws_cdk as cdk

from h100_spot_controller.infrastructure import CapacityControllerStack

def main() -> None:
    app = cdk.App()
    customer_id = app.node.try_get_context("customer_id")
    target_id = app.node.try_get_context("target_id")
    region = app.node.try_get_context("region")
    stack_id = "Phoenix-Codex-Local-Spot-Controller"
    if customer_id is not None:
        stack_id = f"Phoenix-Codex-Local-Spot-{customer_id}-Controller"
        region = region or "ap-south-2"
    CapacityControllerStack(
        app, stack_id, deployment_namespace=customer_id, target_id_default=target_id,
        env=cdk.Environment(region=region) if region is not None else None,
    )
    app.synth()


if __name__ == "__main__":
    main()
