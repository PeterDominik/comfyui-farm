# 0008. Runner environments are installed remotely from reviewed releases, never from scripts sent over the API

Date: 2026-11-15. Status: accepted.

## Context

Updating ComfyUI, node packs and Python environments by hand works for three machines and not for ten. The first idea was an install script sent to the agent through the API, but with identity trusted (0007) that would let anyone on the network run code on every GPU machine.

## Decision

An environment is a folder in this repo: Python version, pinned packages with hashes, named package indexes, an optional setup script, a check command. The manager publishes an immutable, hashed release to the bank folder and tells each agent which release it should have. The agent fetches it, verifies the hash, builds it beside the current one, checks it, switches between jobs and reports. A rollout goes to one machine first and stops there if the check fails. In the pilot ComfyUI is updated by today's `comfyenv` sync, started by the manager on a drained machine.

## Consequences

An agent is set up once per machine. Environment specs are code and stay in git; only policy (0007) is edited in the bank folder. The NVIDIA driver and the first install stay manual. The builder (uv, or the installers' current method with pip) is chosen by a test at the start of phase 3.
