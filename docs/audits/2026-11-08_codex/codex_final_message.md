Wrote [AUDIT_FINDINGS.md](out/AUDIT_FINDINGS.md), [findings.json](out/findings.json), and [raw evidence](out/raw/). Found 26 issues across all 14 branches.

Top 10, ordered by demo risk:

1. **Critical:** Logging failures can prevent `/stop` and leave motion running.
2. **Critical:** Tower-pass entry checks accept one verdict without enforcing confirmation or dwell.
3. Success confirmation can ignore contradictory evidence and bypass the configured count.
4. “Stage-start” images arrive after motion begins, potentially hiding fast successes.
5. Main’s guard is slow and can treat cached images as fresh.
6. One healthy wrist camera masks a missing carrying-arm camera.
7. White-ball thresholds miss neutral white.
8. Fine-tune defaults conflict with generated prompts and hybrid free-text settings.
9. Blocking I/O can carry execution beyond the match deadline.
10. Cloud `--match` accepts timer-only verification and removal of required gates.

All three clones remain unchanged. No real robot, model API, or external service calls were made.