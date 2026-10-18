# MCP

Install once:

```sh
claude mcp add decomp -- decomp --profile /path/to/seat.json mcp
```

The server is a small stdio shim. Every tool call runs the current `decomp` in a fresh interpreter, and the tool list is derived from `decomp describe` each time it is asked for. A change to decomp's commands is therefore live on the next call: no reinstall and no client restart. When the derived tool list changes, the server sends `notifications/tools/list_changed`. Only a change to `src/abu_decomp/mcp.py` itself needs the client to restart the server.

Tools: `decomp_run` takes any decomp argument list; `decomp_<command>` takes the arguments after that command. Each returns the command's JSON result as text and as structured content; a failed command is a tool error.
