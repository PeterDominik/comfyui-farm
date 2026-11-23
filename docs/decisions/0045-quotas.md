# 0045. Quotas on what is queued or running at once, checked at plan

Date: 2026-11-22. Status: accepted.

## Context

0007 lists quotas among the policy's checks. The pilot's GPUs are few and shared by
several shows. Identity is trusted until API keys come (0007): a user name is what the
client sends.

## Decision

- **A quota is a rule in policy.yaml** (`quotas:`), with the same parts as the others
  (name, shows, `except`, reason). It sets `max_jobs`, `max_frames`, or both.
- **It limits what is queued or running at once:** jobs in the active states, from queued
  to postprocessing. It doesn't count what a show used over a day or a week; that is a
  budget, and `job_metrics` (0040) is where one would be counted later.
- **The count is:**
  - per show, by default (`per: show`), each show the rule names on its own;
  - per user over all the rule's shows (`per: user`);
  - for the rule's shows together (`per: all`).
- **A submission is one job:** a job, or a texture set. A set's frames are its unfinished
  batches' tiles, so a half-done set counts what it still has to run. A job's frames are
  its size as the ETAs count it.
- **Checked at the end of `Farm.plan`**, once the job's size is known. The new job counts:
  - one more submission against `max_jobs`;
  - its frames against `max_frames`.

  A job bigger than a show's whole `max_frames` is refused on its own. A submit plans
  under the submit lock, so two submits can't both take the last place. The preview
  passes the user the submit would name, so the artist sees a refusal before submitting.
  A set's Retry failed counts the tiles it puts back.
- **What is queued is read only when a quota is for the job's show.** A plan for a show no
  quota names costs nothing more.
- **The refusal says what is queued, what the most is, the rule, its reason, and what to
  do:** submit again when some have finished, or with fewer frames.

## Consequences

- A quota per user holds only as far as user names are honest until API keys.
- Jobs already queued when a quota is lowered keep running. The quota applies to new
  submissions.
- A cancelled or finished job frees its place at once, since the count is of active jobs.
