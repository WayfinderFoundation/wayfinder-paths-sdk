"""Isolated A/B harness for strategy races and evolution campaigns.

Keep package import deliberately light: the benchmark MCP entry point is loaded
as ``wayfinder_paths.jobs.bench.mcp_server`` inside every arm, and must not
eagerly import the controller that launches that server.
"""
