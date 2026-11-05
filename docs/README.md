# Documentation

Start with the [English README](../README.md) or [Chinese README](../README_CN.md).
See [CHANGELOG](../CHANGELOG.md) for release changes.

## Use and integrate

| Goal | Entry point |
|---|---|
| Save and recall memory from Python | [Quick start](../README.md#quick-start), [public API example](../examples/basic_usage.py) |
| Integrate your agent, OpenClaw, or Hermes | Integration guide: [English](integrations.md), [Chinese](integrations_CN.md) |
| Decide whether a change belongs in memory, Harness, or Octop | [Repository ownership](../AGENTS.md#repository-ownership-and-collaboration), [integration map](agent/PROJECT_MAP.md#repository-integration-map) |
| Upgrade an installation using previous names/paths | Migration guide: [English](integrations.md#renaming-and-migrating-existing-installations), [Chinese](integrations_CN.md#更名与已有安装迁移) |
| Back up, slim, or roll back checkpoint formats | [Checkpoint maintenance](../CONTRIBUTING.md#checkpoint-maintenance) |
| Check supported capabilities and gaps | [Known risks](agent/KNOWN_RISKS.md) |

## Understand and maintain the code

Read the project map, relevant decisions, and test matrix in that order. Consult HANDOFF when continuing a specific task.

| Document | Contents |
|---|---|
| [PROJECT_MAP](agent/PROJECT_MAP.md) | Module entry points, public APIs, write/recall flows, repository ownership, and storage boundaries |
| [DECISIONS](agent/DECISIONS.md) | Stable ADR numbers, design reasons, costs, and supersession relationships |
| [TEST_MATRIX](agent/TEST_MATRIX.md) | Commands by change scope, optional environments, and validation limits |
| [KNOWN_RISKS](agent/KNOWN_RISKS.md) | Current limitations, triggers, and mitigations |
| [GLOSSARY](agent/GLOSSARY.md) | Core terms and easily confused names |
| [README_AUDIT](agent/README_AUDIT.md) | Public claims checked against implementation |
| [HANDOFF](agent/HANDOFF.md) | Code tasks requiring continuation, acceptance criteria, and unresolved items |

See [CONTRIBUTING](../CONTRIBUTING.md) for contribution/branching workflows and [AGENTS](../AGENTS.md) for agent working rules.

## Maintenance and language policy

- Maintain engineering documents (`AGENTS.md`, `CONTRIBUTING.md`, `docs/agent/`) in English as a single source of truth.
- Maintain the README and integration guide in English and Chinese. Link both versions and synchronize contracts, commands, and examples.
- Keep installation, configuration, and troubleshooting in the integration guide; plugin READMEs remain short distribution entry points.
- Put implementation facts in the project map, reasons in ADRs, limitations in known risks, and verification methods in the test matrix.
- Keep HANDOFF focused on continuing work; remove merged tasks from active work and use Issue/PR/Git history for details.
- Avoid copying entire READMEs or test-count baselines. Check code, tests, and configuration rather than judging freshness by document dates.
