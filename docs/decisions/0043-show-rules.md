# 0043. Show rules in policy.yaml, checked at plan; the last valid file stays in force

Date: 2026-11-22. Status: accepted.

## Context

0007 puts the farm's policy in `policy.yaml` in the bank folder, edited by admins and
validated by the manager, which keeps the last valid version. Clients can restrict AI on a
show, entirely or for some kinds of work. Identity is trusted for the pilot, so a rule can
only name shows and definitions, not people.

## Decision

- **Show rules.** Each rule names its shows (case-insensitive, patterns allowed) and a
  reason the artist reads. It does one of three things:
  - `deny: all`;
  - `deny: [definitions]`;
  - `allow: [definitions]`, so only those may run.

  Every rule naming a job's show is checked, and the first that refuses it is named, with
  its reason and the file's `contact`. `other_shows` and `unknown_show` (allow or deny)
  cover shows no rule names and jobs whose show isn't known.
- **Checked in `Farm.plan`**, once the show is known, before any folder is chosen. A refused
  job is never planned or queued: 403, with the rule named. Every way a job is planned
  passes there: preview, submit, Run again, Batch Submit. A set's Retry failed is checked
  too. Queued jobs are not checked again yet (step 4).
- **Validated strictly:**
  - `version: 1`;
  - no unknown keys;
  - unique rule names;
  - each rule denies or allows;
  - every definition a rule names exists in this checkout, so a typo in a deny list can't
    deny nothing.

  `comfyfarm policy check <file>` runs the same validation before a file goes in the bank.
- **Read again when it changes:** the file is looked at no more than every 2 seconds, and
  re-read when its time or size changes.
- **The last valid file stays in force.** A file that doesn't validate, or can't be read,
  isn't taken, and the error is shown (`GET /api/v1/policy`, and the manager's log). The
  manager keeps the last valid file in `data/policy.json`, so a restart with a broken file
  still has it.
- **Fail closed.** With policy on and no valid file ever read, nothing is planned, and the
  refusal says why. A client's restriction must not lapse because of a typo on the first
  day.
- **Off by default** (`policy.enabled`): nothing is read and everything is allowed.

## Consequences

- An admin's typo can stop new jobs only before the first valid file. After that, the
  last valid rules carry on and the error is visible.
- A rule naming a definition that is later removed from the repo makes the file invalid
  on its next read. The last valid version stays in force until the rule is fixed.
- Licences, quotas and a check at dispatch are later steps of phase 7.
