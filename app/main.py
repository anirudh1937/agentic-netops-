from fastapi import FastAPI

app = FastAPI(title="Agentic NetOps")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Agentic NetOps is running"}
