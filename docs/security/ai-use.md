# AI-use note

**Assistant:** Claude Code (Anthropic), used in the terminal on this repository. I asked it to plan before editing, to stop after every step for my review, and to report which tests it actually ran. Commits, pushes, Auth0 changes and the AWS deployment needed my explicit approval.

## What I asked the assistant to do

- Inspect the existing application and propose a minimal ordered plan for the workshop's target architecture, with files, invariant, smallest test and rollback for each step.
- Implement it step by step: fail-closed principal, per-route scope check, persisted ownership with conditional cancellation, principal-scoped idempotency, the JWT authorizer in SAM, the Auth0 SPA login, the contract and the scripts.
- Clean-ups I requested along the way: organize both services into `domain/`, `application/`, `adapters/` and `api/` folders; use standard `HTTPStatus` codes with an error-to-status mapping class; remove hard-coded settings (template parameters as the single source).
- Explain concepts I did not know (iss, sub, aud, scope, principal, PKCE) in plain language.
- Draft this documentation from the code and the executed results.

## What I changed or rejected

- **UI:** I rejected gating the whole SPA behind login once the assistant pointed out that the handout keeps products public. I rejected its first version with several "Log in" buttons and asked for one button, short notes in the cart and orders panels, and a banner when ordering logged out. I asked for a user card showing who is signed in and the granted permissions, and removed a sentence I found unnecessary.
- **Auth0:** the assistant first told me to leave RBAC disabled; after re-reading the handout I enabled RBAC with roles for Alice and Bob and a read-only user, Carol. I also caught that I had given the Auth0 Management API (`…/api/v2/`) as audience; the assistant explained why that was wrong and I used the custom API identifier.
- **Idempotency key:** the assistant prepared the function for me to write; I asked it to write it and explain it line by line.
- **Template:** CloudShell's `sam validate --lint` failed on the empty default of `ExecutionRoleArn` (a false positive of older cfn-lint). Of the options it offered, I chose to remove the default rather than skip `--lint` or disable the rule.
- **Bugs the assistant introduced and fixed after I reported them:** the user card showed "user" because the login did not request `openid profile email`; a mis-counted PASS total in the evidence index.

## What I personally verified

- Configured the Auth0 tenant: API with RS256 and three permissions, RBAC, roles, SPA URLs, users Alice, Bob and Carol.
- Deployed the stack from CloudShell with `sam validate --lint`, `sam build` and `sam deploy --guided`, reviewing the changeset (no IAM resources, `LabRole` reused).
- Signed in as Alice, Bob and Carol in separate browser profiles, checked the granted permissions in the UI, and copied tokens only into environment variables (`read -rs`), never into prompts or files.
- Ran `smoke_test.py` (46 PASS, 0 FAIL, 0 BLOCKED) and `collect_evidence.py` (19 PASS, 0 FAIL) against the deployed API and downloaded the outputs.
- Reviewed each step's diff and explanation before approving the next one.

## What the assistant's output does not prove

Local tests use fabricated trusted-context events and prove Orders' behavior only. The local harness emulates the authorizer without verifying signatures. Gateway authentication is evidenced only by the live smoke run. See [verification.md](verification.md) for the boundary of every claim.
