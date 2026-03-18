"""
Loaders for JSON config files.

All loaders return empty dicts / sensible defaults if the file is missing,
so OptiFlow works out-of-the-box even without config files.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))


def load_business_context() -> dict:
    """Load config/business_context.json.

    Returns {} if the file is missing — in that case Agent Mode works
    without data quality filters or custom terminology.
    """
    path = os.path.join(_CONFIG_DIR, "business_context.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Could not load business_context.json: {e}")
        return {}


def load_model_config() -> dict:
    """Load config/model_config.json.

    Returns {} if the file is missing — callers fall back to hardcoded defaults.
    """
    path = os.path.join(_CONFIG_DIR, "model_config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Could not load model_config.json: {e}")
        return {}
