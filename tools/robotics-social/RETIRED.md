# RETIRED — 2026-07-05

This component is retired and no longer wired into the stack. The code is
kept for reference only.

- It never spoke the real Postiz API — the client here was built against an
  assumed interface and was never validated end-to-end.
- Social posting is now owned by the sibling **soljet-postiz** project; the
  working Postiz posting logic lives there. See its
  `docs/design-per-channel-imagery.md` and this repo's `docs/workbench.md`
  entry 2026-07-05c.

All active wiring was removed on 2026-07-05: the Cloud Run Job, the
`robotics-social-daily` scheduler job, the POSTIZ_* secrets, the
`robotics-social-sa` service account (tools/infra/), the docker-compose
service, the `social` / `social-dry` Make targets, and the deploy.sh image
build + secret pushes.
