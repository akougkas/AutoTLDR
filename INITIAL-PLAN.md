# AutoTLDR: Product & Engineering Vision (v1.0)

This document outlines the strategic vision, technical architecture, and core design principles for the initial version of AutoTLDR. It is the foundational source of truth, intended to guide the engineering team and future AI agents in building the product.

## 1. Vision Statement

To become the essential tool for instantly understanding any complex digital artifact—from codebases to documentation—for both humans and autonomous AI agents.

## 2. Core Architecture: Local-First & Agent-Native

AutoTLDR is founded on a philosophy of privacy, user control, and open standards. Our architecture reflects these core tenets.

*   **Architecture Model: Local-First**
    *   **What:** The primary execution model is `uvx autotldr`. This command launches a local **`fastmcp` server** on the user's machine. All data fetching, processing, and AI analysis happens locally.
    *   **Why:** This is a strategic choice to prioritize user privacy and data security. In an era of cloud-hosted everything, a local-first approach builds fundamental trust with developers. It guarantees that proprietary or sensitive code never leaves the user's machine. This design also enables full offline functionality and delivers a snappy, high-performance experience by eliminating network latency for core operations. The `fastmcp` server, launched in the background, will make AutoTLDR feel like a powerful, native binary, not a web service.

*   **Protocol: MCP-Native**
    *   **What:** The server exposes all its capabilities through the **Model Context Protocol (MCP)**. It is designed from the ground up to be a compliant and robust MCP citizen, correctly implementing primitives like `tools` and `resources`.
    *   **Why:** We are betting on an open, interoperable ecosystem for AI agents. A simple REST API is not enough; it's a static contract. By adhering strictly to the MCP standard, which includes discoverability methods (`*/list`), we create a dynamic interface that agents can *reason* about. This allows an agent to connect to our server and dynamically learn what it can do. This commitment to an open standard ensures AutoTLDR can be used by any compatible agent, preventing vendor lock-in and maximizing its reach. Our goal is to be a quintessential "MCP tool."

## 3. Agent Interaction Model: The "File Handle" API

The core design metaphor for agent interaction is the **"Context ID as a File Handle."** This is our strategic solution to the "context window problem" that plagues large language models.

*   **The Philosophy:** Instead of a single, massive data dump that would overwhelm an agent, we provide a lightweight handle (`session_id`) to a vast, explorable virtual file system. The agent can then intelligently "pull" context as needed, mimicking how a human developer explores a new project: start with the README, list the `src` directory, read a key file, search for a function name, etc. This enables an agent to analyze projects of any size.

### 3.1. The Entry Point: `autotldr.start_session`

An agent initiates an analysis by calling the primary tool: `autotldr.start_session`. This call takes the primary targets (`repo_url`, `doc_url`) and returns the "Initial Bundle," which contains the `session_id`—our "file handle."

### 3.2. The Initial Bundle Schema

The initial payload is strategically designed to provide immediate orientation and entry points for exploration. It is the agent's entire world-view at the start of an interaction.

*   **`key_insights`:** This is the "executive summary" that orients the agent, providing an AI-generated overview and highlighting the most important code and documentation to look at first.
*   **`root_index`:** This serves as the "root directories" (`/code`, `/docs`) of our virtual file system. It provides the agent with the primary resource URIs to begin its deep-dive exploration using the session toolset.

```json
{
  "session_id": "bf2b9b72-5e3c-4e2a-9c2b-4b6d1b7c4a5e",
  "key_insights": {
    "project_summary": "A concise, AI-generated summary of the project's purpose.",
    "core_abstractions": ["A list of key files or concepts central to the project."],
    "golden_path_doc_uri": "autotldr://bf2b9b72.../docs/getting-started"
  },
  "root_index": {
    "code": { "resource_uri": "autotldr://bf2b9b72.../code/" },
    "docs": { "resource_uri": "autotldr://bf2b9b72.../docs/" },
    "issues": { "resource_uri": "autotldr://bf2b9b72.../issues/" }
  },
  "project_pulse": { "is_active": true, "last_commit_date": "..." }
}
```

### 3.3. The Session Toolset: A Virtual File System

Using the `session_id`, the agent can call a set of MCP `tools` that mirror a file system interaction model:

*   `autotldr.list`: The `ls` command for our context bundle. Allows the agent to discover the contents of "directory" resources.
*   `autotldr.read`: The `cat` command. This is an "intelligent read" that returns the content of a resource, potentially summarized by the local LLM if the content is large.
*   `autotldr.search`: The `grep` command. Provides a powerful way to search for keywords or concepts across the entire context bundle.
*   `autotldr.enhance`: The agent's gateway to our server's reasoning capabilities. It allows the agent to request a deeper, custom AI analysis on any piece of context, making our server an interactive reasoning engine.
*   `autotldr.end_session`: The `close` command, allowing the server to clean up the session's temporary resources.

## 4. Core Components & Technical Strategy

### 4.1. Repository Fetching
*   **Strategy:** We will accelerate development by adapting the battle-tested Git cloning and file processing logic from the `rendergit` project. A technical review of `rendergit.py` shows that its `git_clone` function (using `subprocess`) and its file-walking and filtering logic in `collect_files` are robust and can be lifted almost directly, providing a solid foundation for ingesting code.

### 4.2. Documentation Scraper (v1)
*   **Strategy:** For v1, we will build a dedicated in-house scraper to maintain full control over the parsing logic. The integration with external tools like "Context 7" is a future enhancement.
*   **Vision:** The goal is not merely to extract text, but to understand the **semantic structure** of documentation. The scraper will initially target modern documentation frameworks (Docusaurus, VitePress, etc.) to ensure high-quality, structured output. It must identify navigation structures (sidebars, next/previous links) to build a traversable hierarchy. This hierarchy will be exposed via the `autotldr.list` tool, allowing an agent to "navigate" the docs logically, just as a human would.

### 4.3. AI Intelligence Layer
*   **Primary AI Strategy: Local & Deterministic**
    *   **What:** We will utilize a local LLM via a standard inference server like LM Studio. The target model specification is a fast, deterministic model (e.g., "GPTOSS 20B, 32k context") that excels at summarization and data extraction.
    *   **Why:** This is a critical engineering choice for reliability. For the core tasks of AutoTLDR, predictability and speed are more valuable than creativity. This choice minimizes hallucinations, ensures consistent output, and maintains the snappy, responsive feel of a local tool. Our server will interface with this model via its standard OpenAI-compatible API endpoint.

*   **Fallback Strategy: The `Elicitation` Flow**
    *   **What:** If a local AI is not detected, the server will use the MCP `Elicitation` primitive. It will send an `elicitation/requestInput` request, asking the client to provide the summary data itself, based on a schema we provide.
    *   **Why:** This is a cornerstone of our commitment to robustness and the MCP philosophy. After careful consideration, we chose `Elicitation` over `Sampling` because it creates a superior abstraction. Our server's job is to acquire *data* (a summary string); it should not dictate *how* the client acquires it. By sending an `Elicitation` request, we empower the client agent. Its `elicitation_handler` (as defined in the `fastmcp` docs) has full control to fulfill our data request in the way it sees fit—by calling its own powerful cloud LLM, prompting the user, or using a cached result. This makes AutoTLDR a more compliant, robust, and flexible component in a larger agent ecosystem.