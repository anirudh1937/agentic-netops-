import asyncio

from app.mcp.server import mcp


def test_mcp_server_has_health_tool():
    async def run():
        result = await mcp.list_tools()
        return result

    result = asyncio.run(run())

    assert "check_device_health" in str(result)