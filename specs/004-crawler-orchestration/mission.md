# Mission: Orchestrate & trace crawler agent #1

- **Goals:** n8n starts `crawler` runs on a schedule or webhook. Langfuse traces every run, LLM call and tool step. Structured console logs mirror each step.
- **Non-goals:** changing the crawler's logic, letting n8n make routing decisions, hosted deployment.
- **Constraints:** self-hosted; secrets come from env vars only; the harness core imports neither tool.
- **Success:** one run started by n8n shows a complete Langfuse trace and matching console logs. Failures always show up and are never silent.
