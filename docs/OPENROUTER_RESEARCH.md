# OpenRouter benchmark implementation notes

Research and bounded diagnostic completed on 2026-11-08 before implementation.

The local reference is `tools/check_monitor.py`, its `CASES`, `SIM_CASES` and
`sim_frames`, plus `MonitorVerifier.check_now`, `to_openai_messages` and
`harness.hedge.first_of`. The branch is `hedged-calls`. There is no capsule
configuration. The only pre-existing untracked file is the user-supplied brief.
The cable close-ups in `tests/data` label connector types, not stage completion
or illegal-ball possession, so they cannot supply extra benchmark answers.

Supported workflow:

- Build the real monitor request locally, translate text and base64 JPEG blocks
  into Chat Completions messages, then validate that translation without HTTP.
  [OpenRouter vision input formats](https://openrouter.ai/collections/vision-models/)
  support `image_url` content blocks and base64 data URLs on this endpoint.
- Authenticated inference uses a Bearer key. Keep credentials out of reports.
  [Authentication](https://openrouter.ai/docs/api_reference/authentication).
- Read actual `usage.cost` from every completed HTTP attempt, including losing
  race and hedge calls. Usage is automatic; no extra accounting request or
  deprecated include-usage switch is needed.
  [Usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).
- Keep the existing race semantics, first nonempty model response, followed by
  the monitor parser. A hedge selects the first reply accepted by that parser.
  Wait for losing calls for accounting after measuring the winning latency.
- Stop scheduling at the spend threshold or when accounting is incomplete.
  The last concurrent trial can overshoot the threshold; use the server-side
  key credit limit for a separate cap.
  [Key credit limits](https://openrouter.ai/docs/guides/best-practices/capping-shared-agent-keys).
- Use the existing local MuJoCo renderer without starting a policy, station,
  solver or training job. MuJoCo 4.0.0 and the local `yam_mujoco` example are
  available. [MuJoCo Python rendering](https://mujoco.readthedocs.io/en/stable/python.html).

Plausible failures and distinguishing evidence:

| Failure | Smallest diagnostic | Success evidence |
| --- | --- | --- |
| Broken image translation or lost system prompt | Offline build, compare every block and decode JPEG data URLs | Same text, ordering, image bytes and system text |
| Missing scene dependencies or assets | Render the two existing labelled scenes offline | Three valid JPEG camera frames per scene |
| Invalid verdict or truncated reasoning | Feed fake response through the real monitor | Parse failures separate from transport failures and wrong answers |
| Understated race cost | Mock fast winner and slow loser with distinct costs | Both costs counted, latency measured before loser finishes |
| Spend stop bypassed by missing usage | Fake response with absent cost | Unknown cost remains null and scheduling stops |
| Bad key, exhausted credit, provider filtering | User-run single-model, single-repeat benchmark only after dry-run | Report has usage and parsed verdict; no credentials in output |

The first diagnostic built the real `release_cable` photo request, translated it
to one message containing one image, and retained the plan's 4,000-token limit.
It used no network. The public models catalog was read without authentication;
the selected IDs and advertised prices are recorded in `OPENROUTER_SETUP.md`.
No inference, account operation, secret operation or cloud mutation is part of
implementation verification.

The first full suite also exposed an existing scheduling race in
`test_monitor_async_never_blocks_and_one_in_flight`. Its fake transport leaves
the release event set, so the second call can finish before the next poll and
return a new valid answer. Loading the unchanged `HEAD` monitor in memory and
letting that second call complete reproduced the same assertion failure.
[Python Event documentation](https://docs.python.org/3/library/threading.html#event-objects)
confirms that waits do not block while the flag stays set. Reset the fake's
event after each release and explicitly release the second call during cleanup.
This makes the consume-once assertion deterministic without changing monitor
scheduling. Verification is the isolated test, its monitor test group, then the
full suite; the two documented Windows fake-CLI failures remain excluded from
the success criterion.

Final offline verification on this machine:

- `python -m pytest -q`: 354 passed, 1 skipped, 2 known Windows failures in
  `test_claude_cli.py`, both caused by trying to execute the shell-script fake
  CLI as a Win32 program. Log: `runs/judges-dry/pytest-final.txt`.
- New benchmark tests: 32 passed. The combined benchmark, guard, fast-addition
  and corrected async-monitor checks: 49 passed.
- Actual CLI dry-run with `--hedge 1 --hedge 2`: 108 validated trials,
  18 configurations, 180 translated requests, including both real MuJoCo scenes.
  Reports: `runs/judges-dry/summary.csv` and `results.json`.
- Native API and subscription dry-run with hedge 2: 8 photo trials validated
  without constructing authenticated transports or starting Claude.
- `git diff --cached --check` passed. No inference, account creation, credential
  entry, secret/cloud mutation or push was performed.
