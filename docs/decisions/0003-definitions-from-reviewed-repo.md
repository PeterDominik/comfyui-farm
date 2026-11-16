# 0003. Process definitions come only from the reviewed repo and are pinned by hash

Date: 2026-11-14. Status: accepted.

## Context

A process definition decides which command runs on a GPU machine. The pilot trusts the user name a client sends.

## Decision

The farm runs only definitions merged into this repo. Each job records the hash of the definition it was planned with, and an agent refuses a job whose definition it cannot match. Commands come from definitions, never from an API call.

## Consequences

Adding or changing a process is a pull request. Old `manifest.yaml` files keep loading as ComfyUI processes.
