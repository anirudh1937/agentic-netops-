import sys

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.tools.mcp_tool.mcp_toolset import (
    McpToolset,
    StdioConnectionParams,
)
from mcp import StdioServerParameters

from app.agents.tools import diagnose_network


load_dotenv()


mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp.server"],
        ),
        timeout=10.0,
    ),
    tool_filter=["check_device_health"],
)


root_agent = Agent(
    name="netops_agent",
    model="gemini-3.6-flash",
    description="An agent that analyzes network telemetry and explains network health issues.",
    instruction=(
        "You are an expert network operations assistant. "
        "Analyze network telemetry using the diagnose_network tool "
        "and the MCP check_device_health tool when appropriate. "
        "Base your conclusions on returned evidence and anomalies. "
        "Do not invent telemetry values or network conditions. "
        "Clearly explain the detected issue, severity, evidence, "
        "and recommended action in plain English."
    ),
    tools=[diagnose_network, mcp_toolset],
)