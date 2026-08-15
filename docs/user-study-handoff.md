# Genuine user-study handoff

## Current state

The frozen five-person comprehension study has not occurred.
The current state is `HUMAN_VALIDATION_DEFERRED` with zero genuine participants.
The synthetic persona artifact under `evidence/development/user-study` is development-only, counts as zero participants, and must never be copied into genuine evidence.

## Prepare one genuine run

Use a clean checkout of the exact commit that will be recorded in the run.
Confirm that the worktree is clean and the public demo is reproducible:

```sh
git status --short
make bootstrap
make demo-artifacts-check
corepack pnpm build
```

Start the static application in one terminal:

```sh
corepack pnpm --filter @ratereplay/web exec vite preview \
  --host 127.0.0.1 \
  --port 4173
```

Open `http://127.0.0.1:4173/#demo` in a clean browser context.
Do not expose the participant to implementation documentation or private-account screens.

Create the first run record in a separate terminal:

```sh
UV_CACHE_DIR=/private/tmp/rate-replay-uv-cache \
  uv run python scripts/user_comprehension_study.py init \
  --attempt 1 \
  --git-commit "$(git rev-parse HEAD)" \
  --demo-url http://127.0.0.1:4173/#demo \
  --browser-name BROWSER_NAME \
  --browser-version BROWSER_VERSION \
  --output evidence/user-study/m6-comprehension-v1-run-01.json
```

The initializer creates five opaque participant identifiers and deliberately incomplete observations.
It refuses to overwrite an existing run.

## Conduct the five sessions

Recruit exactly five first-time RateReplay users for one run.
Each participant must not have read implementation documents or participated in an earlier run.
Do not collect names, contact information, demographics, employer information, addresses, utility data, account data, or other identifying information.

Follow `studies/user-comprehension/protocol-v1.md` exactly.
Read only its frozen facilitator introduction.
Give the five workflow instructions from `protocol-v1.json` one at a time.
Do not point to controls, define terms, coach, confirm an answer, or show the answer options.

Record for every participant:

- Whether each of the five workflow steps was completed independently.
- Every wrong turn as a short anonymized observation.
- Total duration in seconds.
- A short anonymized paraphrase of each answer.
- The closest frozen answer identifier for each of the five questions.

Include every participant, including failures.
A participant succeeds only by completing every step independently and answering all five questions correctly.
The run passes only when at least four of five participants succeed.

Set a run attestation to `true` only when it is factually accurate.
Do not alter the protocol, rubric, demo hash, failed answers, or participant inclusion to obtain a passing result.

## Validate the genuine result

Run the exact required command:

```sh
make qualification-m6-study
```

A missing, incomplete, stale, coached, selectively reported, or below-threshold run must remain nonzero.
If the run fails, preserve it unchanged, revise the explanation design in a new versioned commit, create the next numbered run linked to the failed run hash, and recruit five fresh first-time participants.
Participant identifiers cannot be reused across attempts.

## Run sequential acceptance checks

After `make qualification-m6-study` passes, run the downstream gates in this order:

```sh
make qualification-m7-restore
make qualification-m7-deployment
make finalize-m8-evaluation
make qualification-m8
make check
make dependency-audit
make clean-checkout-check
make m9-clean-container-check
```

Milestone 6 can move to `ACCEPTED` after its genuine gate passes and the result commit is remotely confirmed.
Milestone 7 can then move from `IMPLEMENTED_PENDING_GATE` to `ACCEPTED` after both local operational qualifications pass on the accepted source.
Milestone 8 can then move to `ACCEPTED` after regenerated evaluation views and `make qualification-m8` pass.
Milestone 9 can move to `ACCEPTED` only after its final clean-checkout, audit, tracked-source, public-claim, and clean-worktree gates also pass.

## Regenerate deferred-state artifacts

Genuine results replace only the deferred human status and any interface changes made in response to a failed run.
Do not rerun performance benchmarks merely because the human evidence changed.

After a passing run:

1. Regenerate `evidence/evaluation/m8-summary.json` and the generated views with `make finalize-m8-evaluation`.
2. Update `docs/evidence/milestone-8.md` and the milestone status ledger in `BUILD_PLAN.md` to reference the genuine run and its derived score.
3. Update README and limitations wording that currently says human validation is deferred.
4. Regenerate the public demo, demo video, protocol hash, and study run only if an explanation-design change modified the demo or frozen protocol.
5. Regenerate `evidence/reproducibility/m9-clean-container.json` from the final remote-confirmed source commit if tracked implementation or documentation changed.

Keep failed run records, synthetic development evidence, and genuine result records clearly separated.
Never describe a synthetic session as a genuine participant.
