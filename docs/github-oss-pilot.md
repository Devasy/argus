# GitHub OSS pilot

The GitHub adapter plugs into `argus/providers/registry.py`. Ingestion, checkout,
review, distillation and audit share the provider contract; GitLab keeps its
existing transport. Another host should implement that contract and register its
adapter. GitHub's saved review delivery is separate from model execution.
Repository and user identities support opaque strings and UUIDs. The historical
numeric GitLab identity columns remain for compatibility with existing data.

## Setup

1. Add `ARGUS_GITHUB_TOKEN` and `GEMINI_API_KEY` to the backend environment
   (`.env.prod` for Docker). Never enter them in the browser or commit them.
   Use a Google AI Studio project **without billing enabled**. Argus cannot
   determine your Google project's billing tier from an API key.
2. Set `ARGUS_GEMINI_FREE_RPM`, `ARGUS_GEMINI_FREE_TPM` and
   `ARGUS_GEMINI_FREE_RPD` below the active limits shown in AI Studio. These
   are local budgets, shared across workers and repositories using the same
   credential reference and model. Chat and embeddings have separate model
   counters. Budget state and server cooldowns persist in PostgreSQL.
3. Start the local stack when needed:

   ```powershell
   docker compose --env-file .env.prod -f docker-compose.prod.yml -f docker-compose.oss.yml up -d --build
   ```

   The normal backend entrypoint applies migrations to the existing DB. Back up
   that DB before starting the updated stack. The override disables automatic
   restarts; stop it using the same Compose files and `stop`. Do not use `down -v`
   on the shared database.
4. In Settings → GitHub connection, verify the posting identity. In
   Settings → Model endpoints, configure or edit `github-gemini-free`, using
   provider `gemini`, model `gemini/gemini-3.8-flash` and credential reference
   `GEMINI_API_KEY`. The **Test** action performs a tool-call round trip and
   consumes two model requests. Model availability depends on your project;
   choose another available Gemini model there if necessary.
5. Import a public PR URL on Repositories. Importing another PR in that repo
   adds only that PR; importing the same URL refreshes it. No repository-wide
   or historical PR backfill runs. Polling requires `ARGUS_POLLER_ENABLED=true`;
   review jobs require `ARGUS_WORKER_ENABLED=true` (both enabled by the Docker
   stack). No publicly reachable server or webhook is required.

## Posting identity

A PAT acts as the normal account that owns it. Creating a machine account does
not produce GitHub's official `[bot]` identity. For arbitrary upstream public
repository contributions, check GitHub's fine-grained PAT limitations; a classic
PAT with `public_repo` is the usual machine-account option. Confirm the account
can comment on the chosen PR before trying to publish from Argus.

For an official `<app-slug>[bot]` identity, register a GitHub App, grant Contents
read and Pull requests read/write permissions, and install it on the target
repository. Configure `ARGUS_GITHUB_APP_ID`, `ARGUS_GITHUB_INSTALLATION_ID` and
`ARGUS_GITHUB_APP_PRIVATE_KEY_PATH=/certs/argus-app.pem`, with the private key in
`./certs/argus-app.pem`. Leave the webhook disabled for polling. Argus signs App
JWTs locally and refreshes expiring installation tokens. Installing the App on
upstream OSS requires its owner's cooperation; a PAT is easier for initial tests.

## Review and knowledge behavior

GitHub imports get a repository-specific Gemini endpoint and 768-dimensional
`gemini-embedding-2` retrieval embeddings. Existing global model and embedding
settings remain available to GitLab. GitHub prompts retrieve only learnings and
file knowledge from the selected repository. Global/internal learnings and
related repositories are excluded. Reviewer profiles and specialist definitions
are shared configuration; choose suitable public-review profiles and avoid
putting confidential content in their instructions.

Every GitHub review creates a saved preview. Inspect its findings and summary,
then select **Publish saved review to GitHub**. Publication submits a `COMMENT`
review with the saved summary and inline comments, without calling Gemini again.
It requires an open, non-draft PR and unchanged head/base/diff. Changes require a
new preview. Unanchorable findings appear in the summary. Multi-line GitLab
suggestion syntax is omitted from GitHub comments; single-line suggestions use
GitHub syntax.

Delivery is serialized per PR and reconciled with an Argus marker. A timeout or
crash after sending is treated as uncertain; **Reconcile delivery** checks for
the remote review and will not blindly post another copy. If GitHub never shows
the review, investigate the delivery before resetting its status administratively.
The provider preflight cannot prevent a push in the tiny interval between its
last read and GitHub accepting a review; comments retain the reviewed commit ID.

Free-only jobs reject model fallback and custom API bases. RPM/TPM waits happen
between requests; a daily-budget limit or API 429 defers the job using `run_after`.
Completed outer review/audit stages resume from their checkpoints. An interrupted
agent conversation or distillation can repeat unfinished model calls; exact
mid-conversation resume is not implemented. Live validation must check total
request use against your actual project's free quota.

## Acceptance checks before relying on it

- Verify identity and Gemini tool-call probe, then import one small real public PR.
- Run a preview; inspect checkout SHA, findings, traces, token counts and quota use.
- Publish on a PR you control; verify summary, inline anchors and bot identity.
- Retry publication and confirm no duplicate review appears.
- Push another commit and confirm an older preview cannot publish.
- Reply to a comment, refresh the PR, and verify reply/thread/reaction ingestion,
  distillation and subsequent repository-scoped retrieval.
- Trigger a learning audit and verify it checks the repository using Gemini.
- Exhaust a small local daily budget, restart the backend, and verify the queued
  job retains its deferred schedule.
- Recheck an existing GitLab repository using its original models and knowledge.

These steps need real credentials and an owner-authorized test PR. Automated
adapter and database tests do not substitute for the live-model pilot.

References: [GitHub PAT documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens),
[GitHub review API](https://docs.github.com/en/rest/pulls/reviews),
[Gemini embeddings](https://ai.google.dev/gemini-api/docs/embeddings),
[Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).


## Optional OpenRouter free endpoint

Set `OPENROUTER_API_KEY` in `.env.prod` and recreate the backend to load it.
Create a model endpoint with provider `openai`, base URL
`https://openrouter.ai/api/v1`, model `openai/cohere/north-mini-code:free`,
and credential reference `OPENROUTER_API_KEY`. Test the endpoint, then select it
as the repository's default endpoint. Existing queued reviews pin their original
endpoint; submit a new preview when changing that selection.

The GitHub pilot accepts only explicit `:free` OpenRouter models, without fallback.
Configure `ARGUS_OPENROUTER_FREE_RPM`, `ARGUS_OPENROUTER_FREE_TPM`, and
`ARGUS_OPENROUTER_FREE_RPD` independently from Gemini (defaults: 5, 131072, 50).
The local daily request budget is capped at 50. Server rate limits defer the job. The advertised model
context can exceed the configured per-request budget; Argus uses the smaller
of its global context setting and the selected provider input budget. Retrieval still uses Gemini embeddings.

See [OpenRouter free limits](https://openrouter.ai/docs/api_reference/limits)
and the [North Mini Code model page](https://openrouter.ai/cohere/north-mini-code:free).
