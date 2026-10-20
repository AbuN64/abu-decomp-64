# Seat tile groups and geometry

`tiles.groups` groups any role IDs, including headless applications, under one
boolean switch. A role belongs to at most one group. All roles are enabled unless
their group explicitly sets `enabled` to `false`:

```json
"groups": {
  "reference": {"enabled": false, "roles": ["reference-app", "reference-video", "reference-console"]}
}
```

Disabled roles are removed before commands are generated or applications and
terminals are spawned. Entire empty rows and columns disappear. Partial holes,
unknown roles, duplicate membership, an empty grid and a disabled focus target
are errors. The original layout and role definitions stay in the profile so the
same switch enables the group at the next full startup.

`tiles.column_weights` and `tiles.row_weights` are positive integer lists for the
original grid. Omitted weights are all 1. Disabled rows and columns lose their
weights, so their space is shared among the remaining ones. A repeated tile spans
the sum of its row weights. One surviving row or column is supported.

An overlay role can declare `pixel_size: [320, 240]`. Its window is sized to the
largest integer multiple of those dimensions that fits its tile after any HUD
reservation. This preserves aspect ratio without fractional compositor scaling.
It does not change the application's framebuffer or texture filtering. A HUD
`height` of 0 reserves no picture space. `tiles.focus` names an enabled role;
its overlay window receives focus when present, otherwise its terminal does.

`decomp --profile SEAT stream apply` stops disabled role runners (which reap their
children), closes their terminals, and regroups and resizes existing enabled
terminals. It preserves enabled apps, the compositor and unrelated clients. It
fails before making changes if an enabled terminal is absent. It does not start
missing roles; enabling a previously stopped group needs the next full startup.
`--watch` keeps overlay geometry and map-time focus current using Sway events.
Normal `stream up` also reads the current profile for every window event.

An already running supervisor from before this feature still owns its old window
subscription and layout in memory. Deploying with `--no-restart` preserves it.
That first migration must retire its old layout subscription before starting
`stream apply --watch`; it must not restart the compositor or playable app.
