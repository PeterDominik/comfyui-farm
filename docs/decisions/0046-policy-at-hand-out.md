# 0046. Show and licence rules checked again when a machine would take a queued job

Date: 2026-11-22. Status: accepted.

## Context

0043 to 0045 check the policy when a job is planned. A job can wait in the queue for
hours, and a client's restriction can arrive in between. 0007 names dispatch as one of the
places the policy is checked.

## Decision

- **When the desk walks the queue** to hand jobs to a machine, each queued job is checked
  against the show and licence rules in force then. It uses its own show and its
  definition's models as the repo has them now. The check comes first, before any machine
  is chosen, so a refused job fails at the next walk even when no machine is free.
- **A refused job fails** before any machine has it, with the error "Not run: the farm's
  policy changed after it was queued." followed by the refusal (the rule, its reason, the
  contact). It never reaches a machine. A set's batches fail one by one, and the set's
  status follows.
- **Quotas aren't asked again.** They decide what may be submitted. A queued job already
  had its place, and failing it because others were queued since would be unfair.
- **A job already on a machine** (preparing, running) runs to the end. Stopping work
  halfway is cancelling, and that stays an artist's or admin's act.
- **A policy that can't be read holds the queue.** With policy on and no valid file ever
  read, a queued job neither fails nor runs. It waits until the file is fixed, and nothing
  is lost to a typo. Once a valid file has been read, the last valid one stays in force
  (0043), so this only happens before the first one.
- **One helper, `Farm.refused_by_policy`,** is used here and by a set's Retry failed.

## Consequences

- A rule written to stop a show takes effect on everything not yet on a machine, within
  the 2 seconds it takes to be read and the next walk of the queue.
- The queue walk does one more cheap check per queued job. Reading the file stays limited
  to once every 2 seconds.
