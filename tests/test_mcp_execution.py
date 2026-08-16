import asyncio

from app.mcp.server import mcp


def test_check_device_health_execution():
    async def run_test():
        result = await mcp.call_tool(
            "check_device_health",
            {
                "device_id": "router-01",
                "interface": "GigabitEthernet0/1",
                "timestamp": "2026-08-16T21:00:00Z",
                "cpu_percent": 95,
                "memory_percent": 70,
                "bandwidth_mbps": 900,
                "packet_loss_percent": 5,
                "latency_ms": 180,
                "interface_status": "up",
            },
        )

        assert result is not None

    asyncio.run(run_test())