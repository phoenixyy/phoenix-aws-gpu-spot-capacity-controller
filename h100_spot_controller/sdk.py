"""Bounded control-plane I/O; never substitute credentials or purchase modes."""

from botocore.config import Config


def client_config() -> Config:
    return Config(
        connect_timeout=3,
        read_timeout=10,
        retries={"total_max_attempts": 2, "mode": "standard"},
        user_agent_extra="gpu-spot-capacity-controller/0.2",
    )
