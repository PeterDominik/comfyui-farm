# 0007. Policy is a file in the bank folder; identity is trusted for the pilot

Date: 2026-11-15. Status: accepted.

## Context

A client can restrict AI use on a show, and model weights carry licences. Few restrictions apply today. Rodeo's sign-on has not been chosen.

## Decision

Deterministic checks run at submit, dispatch, run and output. Show rules, licences and quotas are read from `policy.yaml` in the bank folder beside `manager.json`, edited by admins. The manager validates it and keeps the last valid version. For the pilot the manager keeps trusting the user name a client sends; API keys or Rodeo login come in phase 13.

## Consequences

Until phase 13, quotas and admin-only actions cannot be enforced, so no endpoint may run code it is handed, and environment rollouts start only on the manager's host.
