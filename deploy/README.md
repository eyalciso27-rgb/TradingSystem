# deploy/

Deployment layer of the monorepo (the "infra" area from the task backlog).

Per the Technology and Deployment specification this directory will hold the
Docker Compose stacks (`compose.yaml`, `compose.dev.yaml`, `compose.paper.yaml`,
`compose.observability.yaml`).

They are intentionally **not** created yet: Docker Compose stacks are the scope
of task IMP-0401 (PHASE-4), not IMP-0001. Target platform is Oracle Linux ARM64.
