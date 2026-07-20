# Pi browser-only policy

Pi runs with `--tool ebrowse` expose one custom `ebrowse` tool. Built-in tools and
automatic extension, skill, prompt-template, and context-file discovery are disabled.
The tool accepts one command string, tokenizes quoting with Python `shlex` without
executing expansion/operators, launches a fixed ebrowse target with `shell=False`,
and runs sequentially against the task's browser session.

The standard verb allowlist covers normal observation, navigation, form interaction,
tabs, dialogs, screenshots, and compound browser actions. It excludes `eval`,
`upload`, `connect`, `daemon`, `doctor`, `mcp`, session overrides, `close --all`, and
caller-selected screenshot paths. Argument bytes, result bytes, and call duration are
bounded. A rejected call is recorded as `step.error.class = "policy_block"`, including
the verb and reason; it does not consume a browser-capture slot.

## Configuration

These keys use the normal defaults → benchmark → task → CLI layering and are persisted
fully resolved in `run_meta.config`:

```toml
[config]
navigation_policy = "task-host" # task-host | task-redirects | allowlist | unrestricted
navigation_allowed_domains = [] # additions in task-host/task-redirects; complete in allowlist
navigation_bootstrap_timeout_s = 15
navigation_bootstrap_max_hosts = 5
ebrowse_allowed_verbs = []       # empty = standard browser-only profile
ebrowse_tool_timeout_s = 150
ebrowse_tool_args_max_bytes = 16384
ebrowse_tool_output_max_bytes = 262144
```

CLI equivalents cover common experiment overrides:

```bash
--navigation-policy task-host
--allow-domain login.example.com        # repeatable
--ebrowse-verbs outline,expand,click    # replaces the standard allowlist
--ebrowse-tool-timeout 180
--ebrowse-tool-args-max-bytes 16384
--ebrowse-tool-output-max-bytes 262144
```

`task-host` requires `task.url`, permits that exact hostname and its subdomains, and
adds any `navigation_allowed_domains`. `task-redirects` first opens the trusted task
URL in a temporary bootstrap daemon, permits only public HTTP(S) navigation in that
initial tab, records at most five observed main-frame hosts, and then restarts the
daemon with the original, observed, and explicitly configured domains frozen. Popups
are blocked during discovery. The redirect chain, final URL, errors, and resolved
scope are written to `navigation-bootstrap.json` and `run_meta.config`; the timeout
and host cap are configurable with the two keys above. This is the Online-Mind2Web
smoke benchmark default so regional redirects do not require machine-specific task
exceptions. `allowlist` uses only the configured list.

`unrestricted` permits navigation to any public host (empty allowlist = all
domains). It is a reasonable default when the point of the run is to see where
agents go: the harness is already tightly sandboxed (only the `ebrowse` tool, a
restricted verb allowlist, no `eval`/shell), so an allowlist adds little, and
task-host/task-redirects break tasks whose functionality lives on a sibling
subdomain (e.g. `awt.cbp.gov` from `www.cbp.gov`) or a linked company domain —
scope a redirect chain can't capture, since those are agent-followed links, not
redirects. `unrestricted` still keeps private/loopback blocking on (below) and
does not restore Bash or other Pi tools.

Explicit `open` calls permit only HTTP(S), reject URL credentials, and validate the
hostname. The browser separately restricts top-level document navigation from clicks,
redirects, and popups. It intentionally does not restrict scripts, images, API calls,
or other subresources because modern sites commonly load those cross-domain.

Private and loopback literal targets are blocked under `task-redirects` and
`unrestricted` (a local `task.url`, e.g. a fixture server, opts its own host
out). Dropping the domain allowlist therefore does not also open localhost or
cloud-metadata to the agent. This does not attempt DNS pinning; use
network/container isolation when protection from DNS rebinding or a compromised
browser is required.

## Security boundary

The model has no host shell or file tool, and its arguments never select an executable
or undergo shell expansion. The trusted extension and Python launcher still run as the
current user, and Chromium still processes untrusted sites. Use container or OS-level
isolation if the experiment requires protection from extension, launcher, dependency,
or browser-engine compromise.
