import os

import httpx
from dotenv import load_dotenv


load_dotenv()

DGX_SPARK_BASE_URL = os.getenv(
    "DGX_SPARK_BASE_URL",
    "http://100.123.159.98:8000/v1",
)

DGX_SPARK_MODEL = os.getenv(
    "DGX_SPARK_MODEL",
    "nvidia/Qwen3.6-35B-A3B-NVFP4",
)


def check_qwen_health() -> dict:
    """Check whether the DGX Spark vLLM endpoint is reachable."""
    url = f"{DGX_SPARK_BASE_URL}/models"

    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()

    data = response.json()

    models = [model["id"] for model in data.get("data", [])]

    return {
        "backend": "DGX Spark",
        "provider": "vLLM",
        "model": DGX_SPARK_MODEL,
        "available": DGX_SPARK_MODEL in models,
        "models": models,
    }


def ask_qwen(prompt: str) -> str:
    """Send a prompt to Qwen running on the DGX Spark."""
    url = f"{DGX_SPARK_BASE_URL}/chat/completions"

    payload = {
        "model": DGX_SPARK_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0,
    }

    response = httpx.post(
        url,
        json=payload,
        timeout=60.0,
    )
    response.raise_for_status()

    data = response.json()

    return data["choices"][0]["message"]["content"]