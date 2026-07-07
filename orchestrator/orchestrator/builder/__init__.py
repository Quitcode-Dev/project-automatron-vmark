"""Builder engine.

The sole engine is the Anthropic Agent SDK tool-use loop
(`agent_sdk.implement_issue_via_agent_sdk`). The per-project
llm_config['builder']['engine'] field is retained for forward-compat but only
'agent_sdk' is supported.
"""
