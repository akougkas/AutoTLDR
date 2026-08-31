# Security and data boundaries

AutoTLDR is pre-alpha local software, not a sandbox. It parses untrusted technical files
with bounded adapters and treats extracted text as data, but parser defects and dependency
defects remain possible. Run it with the operating-system permissions appropriate for the
sources it may read.

## Where source data goes

- Local files are copied into private immutable snapshots before extraction. Generated
  outputs may contain source paths, hashes, native metadata, extracted content, and cited
  model claims.
- The configured synthesis endpoint must be loopback-local HTTP. Alpha product setup also
  requires LM Studio's local inventory to prove the exact model instance is active before
  any source is acquired. AutoTLDR sends a bounded
  canonical evidence pack to that endpoint; it does not send the original bulk dataset.
- URL and documentation-site inputs deliberately make outbound HTTP(S) requests. Redirect,
  origin, page, byte, and timeout policies remain bounded.
- Ordinary invocation never downloads, loads, unloads, or chooses a model. Runtime
  lifecycle and physical residency remain outside its authority.

## Agent authority

Shell agents have the same read authority as the process that launches them. The bundled
Agent Skill instructs them to quote paths, set a budget, preserve gaps, and ignore
instructions embedded in source content.

The MCP server is stdio-only and refuses to start without at least one `--root`. Tool
sources must resolve within an authorized root; URLs, stdin, UNC paths, traversal, and
symlink escapes are rejected before acquisition. MCP arguments cannot select a model
endpoint or model ID.

## Extensions

Extensions are executable Python code. AutoTLDR never discovers or imports installed
packages ambiently; a CLI flag or reviewed configuration must name each extension import.
Only enable code you trust.

## Reports

Do not include confidential source content in a public issue. Until a private security
contact is published, report a suspected vulnerability to the repository owner through a
private GitHub security advisory. Include the AutoTLDR version, platform, minimal safe
reproduction, and whether the issue crosses a filesystem, network, model, or output
boundary.
