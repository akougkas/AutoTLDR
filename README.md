# AutoTLDR

![AutoTLDR Banner Placeholder](https://via.placeholder.com/1200x200?text=AutoTLDR+Banner)

**AutoTLDR gives any AI agent an instant understanding of anything.**

In a world of information overload, understanding a new codebase, library, or scientific paper is a monumental task. You have to juggle dozens of tabs, cross-reference documentation with source code, and dig through issues just to get a basic mental model.

AutoTLDR is a local-first, agent-native tool that automates this entire process. It ingests complex sources and synthesizes them into an interactive, explorable context bundle that agents can navigate and reason about.

![AutoTLDR Demo GIF](https://raw.githubusercontent.com/akougkas/autotldr/main/assets/autotldr-demo.gif)

*(This GIF would show a single command `uvx autotldr --repo ... --docs ...` launching the server, and then an agent interacting with it programmatically to explore the context.)*

![AutoTLDR Demo GIF](https://raw.githubusercontent.com/akougkas/autotldr/main/assets/autotldr-demo.gif)
*(This GIF will show a single command `uvx autotldr --repo ... --docs ...` running and instantly opening a stunning, interactive HTML report that links code, docs, and project metrics together.)*

---

## Core Principles

*   **Agent-First:** Built from the ground up as a native **Model Context Protocol (MCP)** tool. Its primary interface is not for humans, but for autonomous agents to call and control.

*   **Local-First & Private:** All data fetching, processing, and AI analysis happens on your machine. Your code, data, and queries never leave your control.

*   **Open Standards:** Fully embraces MCP to ensure broad compatibility and prevent vendor lock-in. AutoTLDR is a bet on a decentralized, interoperable AI ecosystem.

## Core Features

* **Unified Context Document (UCD):** Generates a single, self-contained HTML file or a structured JSON/CXML output that intelligently merges code, documentation, and project activity.

* **"Project Pulse" Dashboard:** Go beyond static code with a dynamic dashboard of project health, including issue velocity, PR-to-merge time, and hot topics in the community.

* **Doc-to-Code Cross-Linker:** The magic that bridges theory and practice. Every function in the code links to its documentation, and every code example in the docs links to its source.

* **AI-Powered "Key Insights":** An automated TL;DR on top of the TL;DR. Uses AI to identify the project's core purpose, its most critical files, and the "golden path" for new users.

* **Interactive "Context-on-Demand":** For massive projects, an interactive mode serves a lightweight dashboard and lazy-loads deep-dive content on demand, giving you the speed of a SaaS app in a local CLI tool.

## How It Works for Agents

AutoTLDR runs as a local MCP server, exposing a powerful set of tools for your agent to use. The core interaction model is the **"File Handle" API**, which allows an agent to explore vast amounts of context without overwhelming its context window.

```python
# A pseudo-code example of an agent using AutoTLDR
from my_agent_framework import MCPClient

# The user starts the AutoTLDR server on their machine
# > uvx autotldr

# The agent connects to the local server
client = MCPClient(port=...)

# 1. Start a session and get the "file handle"
initial_bundle = client.tools.call(
    "autotldr.start_session",
    repo_url="https://github.com/tiangolo/fastapi",
    doc_url="https://fastapi.tiangolo.com/"
)
session_id = initial_bundle["session_id"]
print(initial_bundle["key_insights"]["project_summary"])

# 2. Explore the context using the session_id
docs_root_uri = initial_bundle["root_index"]["docs"]["resource_uri"]
doc_pages = client.tools.call("autotldr.list", session_id=session_id, resource_uri=docs_root_uri)

# 3. Read a specific piece of content
getting_started_content = client.tools.call("autotldr.read", session_id=session_id,
    resource_uri=doc_pages[0]["uri"])
```

## Use Cases

- **AI Agents:** Equip your agent with a powerful tool to gather deep, structured context for code generation, bug fixing, or project analysis.
- **Developers:** Onboard to a new codebase in minutes, not days, by using the human-readable HTML output.
- **Researchers:** Analyze the architecture and evolution of software projects at scale.
- **Technical Writers:** Find discrepancies between documentation and source code automatically.

## Getting Started (for Human Use)

While built for agents, AutoTLDR can also generate a self-contained HTML report for human browsing.

```bash
# Run AutoTLDR and generate an HTML report
uvx autotldr --repo https://github.com/tiangolo/fastapi --docs https://fastapi.tiangolo.com/ --output-html report.html
```

## Our Vision & Contributing

AutoTLDR is more than a tool; it's a new way of interacting with information. We envision a future where no complex topic is out of reach for humans or their AI counterparts.

This is an ambitious open-source project. Check out our `PLAN.md` to see the full vision and how you can get involved.
