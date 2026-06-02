"""Pluggable builder engines.

Today: Aider (subprocess). Experimental: Anthropic Agent SDK (tool-use loop).
Selectable per-project via llm_config['builder']['engine'] = 'aider' | 'agent_sdk'.
The A/B harness in `scripts/compare_builders.py` runs both on the same issue.
"""
