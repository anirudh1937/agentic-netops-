from dotenv import load_dotenv
from google.adk.agents import Agent

from app.agents.tools import diagnose_network


load_dotenv()


root_agent = Agent(
    name="netops_agent",
    model="gemini-3.6-flash",
    description="An agent that analyzes network telemetry and explains network health issues.",
    instruction=(
        "You are an expert network operations assistant. "
        "Analyze network telemetry using the diagnose_network tool. "
        "Base your conclusions on the tool's returned evidence and anomalies. "
        "Do not invent telemetry values or network conditions. "
        "Clearly explain the detected issue, severity, evidence, "
        "and recommended action in plain English."
    ),
    tools=[diagnose_network],
)