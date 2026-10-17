# SRE Agent verifier

The container images [SRE Agent](https://sreagent.app) uses to verify a proposed code fix
before anyone reviews it, and the runner inside them. A verification clones your
repository at a base commit, runs your repository's own commands before and after the
proposed patch, reports what happened and exits.

The same images run on SRE Agent's hosted verifier and, when you choose to run
verification in your own AWS account, on infrastructure you own. This repository is here
so you can read exactly what runs there.

## Images

One per runtime, multi-arch (`amd64` and `arm64`):

| Runtime | Image |
| --- | --- |
| Elixir | `ghcr.io/segfaultpw/sre-agent-verifier-elixir` |
| JavaScript and TypeScript | `ghcr.io/segfaultpw/sre-agent-verifier-node` |
| Python | `ghcr.io/segfaultpw/sre-agent-verifier-python` |
| Go | `ghcr.io/segfaultpw/sre-agent-verifier-go` |
| Java | `ghcr.io/segfaultpw/sre-agent-verifier-java` |
| PHP | `ghcr.io/segfaultpw/sre-agent-verifier-php` |

Pin an image by digest, never by tag. SRE Agent tells you which digests it accepts and,
when one is retired, which one to move to.

## What the runner does, and does not do

- It receives one URL and nothing else. Everything a run needs (a read-only, single
  repository token, the proposed files and the commands) is fetched from that URL, which
  works once and expires in minutes.
- Your repository's commands run as an unprivileged user that cannot read the runner's
  environment, its token or its directory.
- Each command has a time limit and an output limit, and the whole run has a deadline.
- The result is sent to a one-time URL and holds no secret.

The full contract is [`verifier/contract.md`](verifier/contract.md), and the runner is
[`verifier/run.sh`](verifier/run.sh).
The platform side keeps a pinned copy of both files and checks it against this repository, so a
change to the contract is deliberate on both sides.

## How the images are built

Every image is built from a base pinned by digest, contract-tested inside the image
(`verifier/test/contract.sh`), and scanned with Trivy. A fixable HIGH or CRITICAL
vulnerability blocks publishing. The images are rebuilt every week so operating system and
toolchain fixes reach them without anyone touching the Dockerfiles. See
[`.github/workflows/verifier-images.yml`](.github/workflows/verifier-images.yml).

## Running verification in your own account

Use the `verifier` module of
[terraform-sre-agent](https://github.com/segfaultpw/terraform-sre-agent). It deploys a
small poller in your account that asks SRE Agent for work over an outbound HTTPS request
and starts these images. SRE Agent never connects to your account.

## Tests

The tests use the Python standard library only:

```bash
python3 -I -m unittest discover -s tests -v
```

They cover the image sources (every base pinned by digest, the pinned inputs of the node and
python images, the scanner exception), the scripts the Dockerfiles run, and this repository's
workflow. They need `bash`, `git`, `jq`, `openssl` and `tar`; set
`VERIFIER_TESTS_REQUIRE_TOOLS=1` to fail instead of skip when one is missing. The publishing
workflow runs them first, and nothing is built or published when one fails.
`verifier/test/contract.sh RUNTIME` runs the runner contract inside a real image and needs Docker.

## Dependency updates

Dependabot (`.github/dependabot.yml`) proposes new base digests for `verifier/images/*` and
new versions of the GitHub Actions the workflow uses, weekly. The Go image is pinned to a
Go patch release, Node stays on its even (long-term support) lines and the JDK on its
long-term support releases. The weekly rebuild also re-resolves every base digest itself.

## Contributing

This repository is the source of the images and the runner. Issues and pull requests are
welcome. A change to `verifier/run.sh` or `verifier/contract.md` changes what the platform
runs against, so it is made together with the platform's pinned copy.
