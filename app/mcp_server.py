"""Logistics MCP server. Run:  python -m app.mcp_server  (stdio)   or   python -m app.mcp_server --http"""
import sys
try:
    from mcp.server.mcpserver import MCPServer as FastMCP   # mcp 2.x
except ImportError:
    from mcp.server.fastmcp import FastMCP                   # mcp 1.x
from . import db, tools, seed

db.init()
if not db.one("SELECT 1 x FROM vehicles"):
    seed.seed()

mcp = FastMCP("dock-to-door-logistics")
for fn in (tools.get_shipment_state, tools.get_telemetry_summary, tools.recall_similar_incidents, tools.find_alternate_vehicles,
           tools.scan_fleet_anomalies, tools.reroute, tools.reserve_dock_slot, tools.notify, tools.log_decision, tools.write_rule):
    mcp.tool()(fn)

if __name__ == "__main__":
    mcp.run(transport="streamable-http" if "--http" in sys.argv else "stdio")
