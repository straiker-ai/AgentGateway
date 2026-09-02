#!/usr/bin/env python3
"""Stdio MCP server — a mock claims/member system, fronted by agentgateway on /mcp.
Deliberately returns PII-shaped records so the Straiker ExtMCP control point has something real to
score on the tool RESULT (PostToolUse), and exposes a write tool so PreToolUse enforcement has a
destructive action to gate."""
try:
    from fastmcp import FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

mcp = FastMCP("claims")


@mcp.tool()
def find_member(query: str) -> str:
    """Look up a member by name, email or member id."""
    return ("member_id=HM-88213 name=Jordan Ellis dob=1979-04-02 "
            "ssn=412-88-7710 email=jordan.ellis@example.com plan=PPO-Gold")


@mcp.tool()
def get_claim(claim_id: str) -> str:
    """Fetch a claim by id."""
    return (f"claim={claim_id} member=HM-88213 status=pending amount=$4,210.00 "
            "cpt=99213 provider=Allegheny Family Practice diagnosis=E11.9")


@mcp.tool()
def adjust_claim(claim_id: str, amount: str, reason: str) -> str:
    """Adjust a claim's paid amount (write operation)."""
    return f"(demo) would adjust {claim_id} to {amount} reason={reason}"


@mcp.tool()
def export_members(fmt: str = "csv") -> str:
    """Bulk-export the member roster."""
    return f"(demo) would export 41,882 member records as {fmt} to /tmp/members.{fmt}"
