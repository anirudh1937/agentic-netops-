import asyncio

from app.agents.agent import root_agent


def test_adk_discovers_mcp_tool():
    mcp_toolset = root_agent.tools[1]

    async def run():
        tools = await mcp_toolset.get_tools(None)
        return tools

    tools = asyncio.run(run())

    names = [tool.name for tool in tools]

    assert "check_device_health" in names