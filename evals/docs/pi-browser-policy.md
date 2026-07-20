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
navigation_policy = "task-host" # task-host | allowlist | unrestricted
navigation_allowed_domains = [] # additions in task-host; complete list in allowlist
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
adds any `navigation_allowed_domains`. `allowlist` uses only the configured list.
`unrestricted` is an explicit escape hatch for experiments that need cross-site
navigation; it does not restore Bash or other Pi tools.

Explicit `open` calls permit only HTTP(S), reject URL credentials, and validate the
hostname. The browser separately restricts top-level document navigation from clicks,
redirects, and popups. It intentionally does not restrict scripts, images, API calls,
or other subresources because modern sites commonly load those cross-domain.

## Security boundary

The model has no host shell or file tool, and its arguments never select an executable
or undergo shell expansion. The trusted extension and Python launcher still run as the
current user, and Chromium still processes untrusted sites. Use container or OS-level
isolation if the experiment requires protection from extension, launcher, dependency,
or browser-engine compromise.
