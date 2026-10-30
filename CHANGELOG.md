# Changelog

## v0.1.6 - 2026-10-30

- ci: stop inferring major from PR titles; pin third-party actions by SHA (#20) (1c81b40)

## v0.1.5 - 2026-10-30

- fix: key the cache by per-analyzer algorithm version, not the package version (#19) (2ce04e0)

## v0.1.4 - 2026-10-30

- docs: refresh STATE.md against main (roadmap step 2) (#18) (95c7dee)

## v0.1.3 - 2026-10-30

- fix: report the release version, single-sourced from pyproject.toml (#17) (8a9964f)

## v0.1.2 - 2026-10-30

- Add ECOSYSTEM_ROADMAP.md: gaps, wiring and PR-sized steps from the 2026-10-30 ecosystem audit (#16) (64903a2)

## v0.1.1 - 2026-10-23

- ci: bump GitHub Actions, and resolve release versions with release-drafter v7 dry-run (#15) (b1d14f9)

## v0.1.0 - 2026-10-23

- Add GitHub automation: release, PR autolabel, Dependabot, CodeQL, PR template, SECURITY.md (#9) (50a9fe9)
- ci: run the Python 3.9 jobs on ubuntu-24.04 (#11) (beacdfb)
- docs(readme): fresnel-style header with a Liquid Glass app icon (#10) (0eb873f)
- STATE.md: video-production-agent adapter already exists, not blocked (8cc5745)
- Document conformance in SKILL.md, add it to CI, record cross-repo verification (ab93ee7)
- Add self-conformance checks: media-analysis conformance --json (SKILL_SPEC.md section 8) (50c78b4)
- Fix JSON protocol on non-UTF-8 pipes: emit UTF-8 bytes, read stdin as UTF-8 (e495a47)
- OS conformance hardening: error classes, recursive denylist, provides drift check, per-Capability doctor, durable state (5ed93a1)
- Fix: nine -> ten analysis kinds (plain counting error) (a2d4ba8)
- provides: cover the remaining 4 analysis kinds now that the matrix decided (c67ecdd)
- Add provides: publish Capability ids for cross-repository discovery (bd7d639)
- README landing page: official hero visual, badges, quick start, ecosystem flow (#3) (a9ad059)
- Add GitHub Sponsors funding link and Support section (491d40a)
- Agent-ready contract hardening: self-validation, drift fixtures, protocol and media matrix tests (26593e8)
- Fix Windows process-group kill and a clock-resolution-dependent budget test (530e483)
- Harden the Skill contract for video-production-agent integration (0c03298)
- Implement media-analysis-skill 0.1.0: deterministic media observation Skill (253fab7)
- Initial commit (d5c6c13)

