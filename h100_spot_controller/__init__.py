"""GPU Spot Capacity Controller; legacy package and CLI names remain compatible."""

__version__ = "0.2.0"

from .config import CapacityTarget, ConfigurationError, load_target

__all__ = ["CapacityTarget", "ConfigurationError", "load_target"]
